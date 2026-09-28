package com.olympuscontrols.realsensepilot;

import java.net.URI;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * The URScript a RealSense Pick node contributes to the program — generated here, with
 * no UR API, so it is tested on any JDK. It runs inside the operator's program on the
 * controller, so it works in Local mode and needs no Primary client:
 *
 * <ol>
 *   <li>force the TCP to the flange (the pick server answers in flange poses; the
 *       active TCP is restored before the operator's gripper nodes and at the end);</li>
 *   <li>{@code movej} to the taught survey joints if there are any — otherwise the first
 *       look is from wherever the arm is;</li>
 *   <li>open a socket to the pick server, send {@code FIND} with the flange pose and
 *       the taught tap pixel, read {@code (status, centre, flange pose)};</li>
 *   <li>ask {@code LOOK} for the closer look: the camera halfway to the block, aimed
 *       at it ({@code movej} to the controller's own IK solution; straight over the
 *       block when the server has none), then {@code REFINE} — straight down first,
 *       then leaned 12° and 24° outward while the controller's IK cannot solve hover +
 *       grip + lift;</li>
 *   <li>{@code movel} to hover, slowly to the grip depth, run the child nodes (the
 *       operator's gripper Close), lift along the tool axis.</li>
 * </ol>
 *
 * With the part's rough size set (length × width as it lies, optionally its height, and a
 * tolerance), FIND and REFINE carry {@code part=LxWxH tol=T}: the pick server considers
 * only candidates that size, and the grip depth is refused if it would reach the table.
 *
 * Every stage is reported twice: {@code textmsg} (PolyScope's Log tab) and a {@code LOG}
 * line to the pick server, which writes it with its own answers into one timestamped
 * log on the cockpit's machine. The result variable is True only after a completed
 * lift; anything that stops short says why in a blocking popup (when {@link #popupOnFail})
 * and leaves it False — an If on it decides what happens next.
 */
final class PickScript {
    static final String VERSION = "0.4.0";
    static final int DEFAULT_PICK_PORT = 7622;
    static final String SOCKET = "rs_pick";
    /** The pick server's status codes, as the operator should read them. */
    static final String[][] REASONS = {
        {"0", "no block in view"},
        {"-1", "the block is wider than the gripper can open"},
        {"-2", "the block is too close to the robot base"},
        {"-3", "the cockpit has no hand-eye calibration (or cannot be reached)"},
        {"-4", "no fresh camera frame - is the cockpit's camera streaming?"},
        {"-5", "the closer look did not find the block again"},
        {"-7", "something is in view, but nothing the size of the part"},
        {"-8", "no answer from the pick server within 10 s"},
        {"-9", "the pick server did not understand the request"},
    };

    String host = "";
    int port = DEFAULT_PICK_PORT;
    double[] surveyJoints; // rad; null: the first look is from where the arm is
    int tapU = -1;
    int tapV = -1;
    double gripBelowTopMm = 15.0;
    double liftMm = 60.0;
    double hoverMm = 40.0;
    double lookMm = 90.0;
    /** The part's rough size as it lies, mm; 0 x 0: any block-sized thing. Height 0: not checked. */
    double partLengthMm = 0.0;
    double partWidthMm = 0.0;
    double partHeightMm = 0.0;
    double partTolPct = 25.0;
    boolean popupOnFail = true;
    String foundVariable = "rs_pick_found";

    /** The cockpit host from its saved URL (the Installation node's field); "" if none. */
    static String hostOf(String cockpitBase) {
        try {
            String h = new URI(cockpitBase).getHost();
            return h == null ? "" : h;
        } catch (Exception e) {
            return "";
        }
    }

    /** Why the node cannot generate a program yet, or null when it can. */
    String problem() {
        if (surveyJoints != null) {
            if (surveyJoints.length != 6) return "the survey position is not a joint position";
            for (double q : surveyJoints) {
                if (Double.isNaN(q) || Double.isInfinite(q)) return "the survey position is not a joint position";
            }
        }
        if (host.isEmpty()) return "set the cockpit address in Installation → URCaps → RealSense Pilot";
        if (!host.matches("[A-Za-z0-9.:\\-]+")) return "the cockpit host \"" + host + "\" is not an address";
        if (port < 1 || port > 65535) return "the pick port must be 1..65535";
        if (!(gripBelowTopMm >= 0 && gripBelowTopMm <= 60)) return "the grip depth must be 0..60 mm below the top";
        if (!(liftMm >= 5 && liftMm <= 300)) return "the lift must be 5..300 mm";
        String part = partProblem();
        if (part != null) return part;
        if (!foundVariable.matches("[A-Za-z_][A-Za-z0-9_]*")) return "the result variable name is not valid";
        return null;
    }

    boolean hasPart() {
        return partLengthMm > 0 || partWidthMm > 0 || partHeightMm > 0;
    }

    private String partProblem() {
        if (!hasPart()) return null;
        if (!(partLengthMm > 0 && partWidthMm > 0)) return "give the part's length and width (or clear the part size)";
        if (!dim(partLengthMm) || !dim(partWidthMm) || !(partHeightMm == 0 || dim(partHeightMm))) {
            return "part dimensions must be 5..500 mm";
        }
        if (!(partTolPct >= 5 && partTolPct <= 100)) return "the part tolerance must be 5..100 %";
        if (partHeightMm > 0 && gripBelowTopMm > partHeightMm - 2) {
            return "the grip depth (" + num(gripBelowTopMm) + " mm) would put the fingertips on the table: the part is "
                    + num(partHeightMm) + " mm tall";
        }
        return null;
    }

    private static boolean dim(double mm) {
        return mm >= 5 && mm <= 500;
    }

    /** {@code " part=60x40x30 tol=25"} for the pick server, or "" when no part size is set. */
    String partToken() {
        if (!hasPart()) return "";
        return " part=" + num(Math.max(partLengthMm, partWidthMm)) + "x" + num(Math.min(partLengthMm, partWidthMm))
                + (partHeightMm > 0 ? "x" + num(partHeightMm) : "") + " tol=" + num(partTolPct);
    }

    /** "60 x 40 x 30 mm +-25 %" — ASCII, for the header comment and the Log tab. */
    String partText() {
        if (!hasPart()) return "any block";
        return num(Math.max(partLengthMm, partWidthMm)) + " x " + num(Math.min(partLengthMm, partWidthMm))
                + (partHeightMm > 0 ? " x " + num(partHeightMm) : "") + " mm +-" + num(partTolPct) + " %";
    }

    /** One decimal at most, no trailing zeros, never a locale's comma. */
    static String num(double v) {
        String t = String.format(Locale.ROOT, "%.1f", v);
        return t.endsWith(".0") ? t.substring(0, t.length() - 2) : t;
    }

    /** The lines before the child nodes (they run at the grip, with the active TCP). */
    List<String> beforeChildren() {
        String why = problem();
        if (why != null) throw new IllegalStateException(why);
        List<String> s = new ArrayList<String>();
        s.add("# RealSense Pick " + VERSION + " - pick server " + host + ":" + port + " - part " + partText());
        s.add(foundVariable + " = False");
        s.add("rs_tcp0 = get_tcp_offset()");
        s.add("set_tcp(p[0, 0, 0, 0, 0, 0])");
        if (surveyJoints != null) {
            say(s, "", "to the survey position", null, false);
            s.add("movej(" + joints(surveyJoints) + ", a=1.2, v=0.5)");
            s.add("sleep(0.6)");
        } else {
            say(s, "", "first look from where the arm is", null, false);
        }
        String part = partToken();
        s.add("rs_go = False");
        s.add("rs_st = -8");
        s.add("rs_why = \"no answer from the pick server within 10 s\"");
        s.add("if socket_open(\"" + host + "\", " + port + ", \"" + SOCKET + "\"):");
        say(s, "  ", "start, flange ", "get_actual_tcp_pose()", true);
        say(s, "  ", "looking for " + partText(), null, true);
        s.add("  socket_send_line(str_cat(\"FIND \", str_cat(to_str(get_actual_tcp_pose()), \" u="
                + tapU + " v=" + tapV + part + "\")), \"" + SOCKET + "\")");
        s.add("  rs_r = socket_read_ascii_float(10, \"" + SOCKET + "\", 10)");
        s.add("  if rs_r[0] == 10:");
        s.add("    rs_st = rs_r[1]");
        s.add("  end");
        say(s, "  ", "FIND status ", "rs_st", true);
        s.add("  if rs_st == 1:");
        s.add("    rs_c = p[rs_r[2], rs_r[3], rs_r[4], 0, 0, 0]");
        s.add("    rs_top = p[rs_r[5], rs_r[6], rs_r[7], rs_r[8], rs_r[9], rs_r[10]]");
        s.add("    rs_look = pose_trans(rs_top, p[0, 0, " + m(-lookMm) + ", 0, 0, 0])");
        s.add("    socket_send_line(str_cat(\"LOOK \", str_cat(to_str(get_actual_tcp_pose()), str_cat(\" \","
                + " to_str(rs_c)))), \"" + SOCKET + "\")");
        s.add("    rs_r = socket_read_ascii_float(10, \"" + SOCKET + "\", 10)");
        // index rs_r[1] only after a full read: a timed-out read has nothing there
        s.add("    rs_ls = -8");
        s.add("    if rs_r[0] == 10:");
        s.add("      rs_ls = rs_r[1]");
        s.add("    end");
        s.add("    if rs_ls == 1:");
        s.add("      rs_look = p[rs_r[5], rs_r[6], rs_r[7], rs_r[8], rs_r[9], rs_r[10]]");
        say(s, "      ", "LOOK halfway to the block ", "rs_look", true);
        s.add("    else:");
        say(s, "      ", "LOOK from straight over the block ", "rs_look", true);
        s.add("    end");
        s.add("    if get_inverse_kin_has_solution(rs_look, get_actual_joint_positions()):");
        s.add("      movej(get_inverse_kin(rs_look, get_actual_joint_positions()), a=1.2, v=0.5)");
        s.add("      sleep(0.5)");
        say(s, "      ", "at the look pose", null, true);
        s.add("    else:");
        say(s, "      ", "the look pose is out of reach - looking again from here", null, true);
        s.add("    end");
        s.add("    rs_leans = [0, 12, 24]");
        s.add("    rs_try = 0");
        s.add("    while (rs_try < 3) and (rs_go == False):");
        s.add("      socket_send_line(str_cat(\"REFINE \", str_cat(to_str(get_actual_tcp_pose()), str_cat(\" \","
                + " str_cat(to_str(rs_c), str_cat(\"" + part + " lean=\", to_str(rs_leans[rs_try])))))), \"" + SOCKET
                + "\")");
        s.add("      rs_r = socket_read_ascii_float(10, \"" + SOCKET + "\", 10)");
        s.add("      rs_rs = -8");
        s.add("      if rs_r[0] == 10:");
        s.add("        rs_rs = rs_r[1]");
        s.add("      end");
        s.add("      if rs_rs == 1:");
        s.add("        rs_c = p[rs_r[2], rs_r[3], rs_r[4], 0, 0, 0]");
        s.add("        rs_top = p[rs_r[5], rs_r[6], rs_r[7], rs_r[8], rs_r[9], rs_r[10]]");
        s.add("      end");
        say(s, "      ", "REFINE status ", "rs_rs", true);
        s.add("      rs_hover = pose_trans(rs_top, p[0, 0, " + m(-hoverMm) + ", 0, 0, 0])");
        s.add("      rs_grip = pose_trans(rs_top, p[0, 0, " + m(gripBelowTopMm) + ", 0, 0, 0])");
        s.add("      rs_lift = pose_trans(rs_top, p[0, 0, " + m(-liftMm) + ", 0, 0, 0])");
        s.add("      rs_q = get_actual_joint_positions()");
        s.add("      if get_inverse_kin_has_solution(rs_hover, rs_q) and get_inverse_kin_has_solution(rs_grip, rs_q)"
                + " and get_inverse_kin_has_solution(rs_lift, rs_q):");
        s.add("        rs_go = True");
        s.add("      else:");
        say(s, "        ", "no IK for hover + grip + lift at lean ", "rs_leans[rs_try]", true);
        s.add("      end");
        s.add("      rs_try = rs_try + 1");
        s.add("    end");
        s.add("    if rs_go:");
        say(s, "      ", "hover ", "rs_hover", true);
        s.add("      movel(rs_hover, a=0.8, v=0.25)");
        say(s, "      ", "down to the grip ", "rs_grip", true);
        s.add("      movel(rs_grip, a=0.3, v=0.05)");
        s.add("      set_tcp(rs_tcp0)");
        say(s, "      ", "gripper nodes", null, true);
        return s;
    }

    /** The lines after the child nodes. */
    List<String> afterChildren() {
        List<String> s = new ArrayList<String>();
        s.add("      set_tcp(p[0, 0, 0, 0, 0, 0])");
        say(s, "      ", "lift", null, true);
        s.add("      movel(rs_lift, a=0.5, v=0.1)");
        s.add("      " + foundVariable + " = True");
        say(s, "      ", "picked", null, true);
        s.add("    else:");
        s.add("      rs_why = \"no approach the arm can reach (straight down or leaned to 24 deg)\"");
        s.add("    end");
        s.add("  else:");
        s.add("    rs_why = str_cat(\"pick server status \", to_str(rs_st))");
        for (int i = 0; i < REASONS.length; i++) {
            s.add("    " + (i == 0 ? "if" : "elif") + " rs_st == " + REASONS[i][0] + ":");
            s.add("      rs_why = \"" + REASONS[i][1] + "\"");
        }
        s.add("    end");
        s.add("  end");
        s.add("  if " + foundVariable + " == False:");
        s.add("    textmsg(\"RealSense Pick: no pick - \", rs_why)");
        s.add("    socket_send_line(str_cat(\"LOG no pick - \", rs_why), \"" + SOCKET + "\")");
        if (popupOnFail) {
            s.add("    popup(str_cat(\"RealSense Pick: no pick - \", rs_why), \"RealSense Pick\", False, True,"
                    + " blocking=True)");
        }
        s.add("  end");
        s.add("  socket_close(\"" + SOCKET + "\")");
        s.add("else:");
        s.add("  textmsg(\"RealSense Pick: no pick server at " + host + ":" + port + "\", \"\")");
        s.add("  popup(\"RealSense Pick: no pick server at " + host + ":" + port
                + " - start the cockpit (or perception pick-server) there and allow it through the"
                + " firewall\", \"RealSense Pick\", False, True, blocking=True)");
        s.add("end");
        s.add("set_tcp(rs_tcp0)");
        return s;
    }

    /**
     * One stage report: {@code textmsg} for the Log tab and, once the socket is open, a
     * {@code LOG} line to the pick server. {@code text} is a constant this class writes
     * (ASCII, no quotes); {@code value} an optional URScript expression.
     */
    private static void say(List<String> s, String indent, String text, String value, boolean socket) {
        String v = value == null ? "\"\"" : value;
        s.add(indent + "textmsg(\"RealSense Pick: " + text + "\", " + v + ")");
        if (socket) {
            String line = value == null ? "\"LOG " + text + "\"" : "str_cat(\"LOG " + text + "\", to_str(" + value + "))";
            s.add(indent + "socket_send_line(" + line + ", \"" + SOCKET + "\")");
        }
    }

    /** The whole contribution with {@code children} where the child nodes go — for tests and previews. */
    String render(String children) {
        StringBuilder b = new StringBuilder();
        for (String l : beforeChildren()) b.append(l).append('\n');
        if (children != null && !children.isEmpty()) b.append(children).append('\n');
        for (String l : afterChildren()) b.append(l).append('\n');
        return b.toString();
    }

    private static String joints(double[] q) {
        StringBuilder b = new StringBuilder("[");
        for (int i = 0; i < q.length; i++) {
            if (i > 0) b.append(", ");
            b.append(String.format(Locale.ROOT, "%.6f", q[i]));
        }
        return b.append(']').toString();
    }

    private static String m(double mm) {
        return String.format(Locale.ROOT, "%.4f", mm / 1000.0);
    }
}
