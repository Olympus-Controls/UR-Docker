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
 *   <li>{@code movej} to the taught survey joints, settle;</li>
 *   <li>open a socket to the cockpit's pick server, send {@code FIND} with the flange
 *       pose and the taught tap pixel, read {@code (status, centre, flange pose)};</li>
 *   <li>move over the block for a closer look ({@code movej} to the controller's own IK
 *       solution), {@code REFINE} — straight down first, then leaned 12° and 24° outward
 *       while the controller's IK cannot solve hover + grip + lift;</li>
 *   <li>{@code movel} to hover, slowly to the grip depth, run the child nodes (the
 *       operator's gripper Close), lift along the tool axis.</li>
 * </ol>
 *
 * The result variable is True only after a completed lift; everything that stops short
 * leaves it False, says why with {@code textmsg}, and the program carries on — an If on
 * it decides what happens next. A cockpit that cannot be reached raises a blocking popup.
 */
final class PickScript {
    static final String VERSION = "0.2.0";
    static final int DEFAULT_PICK_PORT = 7622;
    static final String SOCKET = "rs_pick";

    String host = "";
    int port = DEFAULT_PICK_PORT;
    double[] surveyJoints; // rad
    int tapU = -1;
    int tapV = -1;
    double gripBelowTopMm = 15.0;
    double liftMm = 60.0;
    double hoverMm = 40.0;
    double lookMm = 90.0;
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
        if (surveyJoints == null || surveyJoints.length != 6) return "teach the survey position";
        for (double q : surveyJoints) {
            if (Double.isNaN(q) || Double.isInfinite(q)) return "the survey position is not a joint position";
        }
        if (host.isEmpty()) return "set the cockpit address in Installation → URCaps → RealSense Pilot";
        if (!host.matches("[A-Za-z0-9.:\\-]+")) return "the cockpit host \"" + host + "\" is not an address";
        if (port < 1 || port > 65535) return "the pick port must be 1..65535";
        if (!(gripBelowTopMm >= 0 && gripBelowTopMm <= 60)) return "the grip depth must be 0..60 mm below the top";
        if (!(liftMm >= 5 && liftMm <= 300)) return "the lift must be 5..300 mm";
        if (!foundVariable.matches("[A-Za-z_][A-Za-z0-9_]*")) return "the result variable name is not valid";
        return null;
    }

    /** The lines before the child nodes (they run at the grip, with the active TCP). */
    List<String> beforeChildren() {
        String why = problem();
        if (why != null) throw new IllegalStateException(why);
        List<String> s = new ArrayList<String>();
        s.add("# RealSense Pick " + VERSION + " - cockpit pick server " + host + ":" + port);
        s.add(foundVariable + " = False");
        s.add("rs_tcp0 = get_tcp_offset()");
        s.add("set_tcp(p[0, 0, 0, 0, 0, 0])");
        s.add("movej(" + joints(surveyJoints) + ", a=1.2, v=0.5)");
        s.add("sleep(0.6)");
        s.add("rs_go = False");
        s.add("rs_st = -8");
        s.add("if socket_open(\"" + host + "\", " + port + ", \"" + SOCKET + "\"):");
        s.add("  socket_send_line(str_cat(\"FIND \", str_cat(to_str(get_actual_tcp_pose()), \" u="
                + tapU + " v=" + tapV + "\")), \"" + SOCKET + "\")");
        s.add("  rs_r = socket_read_ascii_float(10, \"" + SOCKET + "\", 10)");
        s.add("  if rs_r[0] == 10:");
        s.add("    rs_st = rs_r[1]");
        s.add("  end");
        s.add("  if rs_st == 1:");
        s.add("    rs_c = p[rs_r[2], rs_r[3], rs_r[4], 0, 0, 0]");
        s.add("    rs_top = p[rs_r[5], rs_r[6], rs_r[7], rs_r[8], rs_r[9], rs_r[10]]");
        s.add("    rs_look = pose_trans(rs_top, p[0, 0, " + m(-lookMm) + ", 0, 0, 0])");
        s.add("    if get_inverse_kin_has_solution(rs_look, get_actual_joint_positions()):");
        s.add("      movej(get_inverse_kin(rs_look, get_actual_joint_positions()), a=1.2, v=0.5)");
        s.add("      sleep(0.5)");
        s.add("    end");
        s.add("    rs_leans = [0, 12, 24]");
        s.add("    rs_try = 0");
        s.add("    while (rs_try < 3) and (rs_go == False):");
        s.add("      socket_send_line(str_cat(\"REFINE \", str_cat(to_str(get_actual_tcp_pose()), str_cat(\" \","
                + " str_cat(to_str(rs_c), str_cat(\" lean=\", to_str(rs_leans[rs_try])))))), \"" + SOCKET + "\")");
        s.add("      rs_r = socket_read_ascii_float(10, \"" + SOCKET + "\", 10)");
        s.add("      if (rs_r[0] == 10) and (rs_r[1] == 1):");
        s.add("        rs_c = p[rs_r[2], rs_r[3], rs_r[4], 0, 0, 0]");
        s.add("        rs_top = p[rs_r[5], rs_r[6], rs_r[7], rs_r[8], rs_r[9], rs_r[10]]");
        s.add("      end");
        s.add("      rs_hover = pose_trans(rs_top, p[0, 0, " + m(-hoverMm) + ", 0, 0, 0])");
        s.add("      rs_grip = pose_trans(rs_top, p[0, 0, " + m(gripBelowTopMm) + ", 0, 0, 0])");
        s.add("      rs_lift = pose_trans(rs_top, p[0, 0, " + m(-liftMm) + ", 0, 0, 0])");
        s.add("      rs_q = get_actual_joint_positions()");
        s.add("      if get_inverse_kin_has_solution(rs_hover, rs_q) and get_inverse_kin_has_solution(rs_grip, rs_q)"
                + " and get_inverse_kin_has_solution(rs_lift, rs_q):");
        s.add("        rs_go = True");
        s.add("      end");
        s.add("      rs_try = rs_try + 1");
        s.add("    end");
        s.add("    if rs_go:");
        s.add("      movel(rs_hover, a=0.8, v=0.25)");
        s.add("      movel(rs_grip, a=0.3, v=0.05)");
        s.add("      set_tcp(rs_tcp0)");
        return s;
    }

    /** The lines after the child nodes. */
    List<String> afterChildren() {
        List<String> s = new ArrayList<String>();
        s.add("      set_tcp(p[0, 0, 0, 0, 0, 0])");
        s.add("      movel(rs_lift, a=0.5, v=0.1)");
        s.add("      " + foundVariable + " = True");
        s.add("    else:");
        s.add("      textmsg(\"RealSense Pick: no approach the arm can reach (straight down or leaned to 24 deg)\")");
        s.add("    end");
        s.add("  else:");
        s.add("    textmsg(\"RealSense Pick: no pick, status \", rs_st)");
        s.add("  end");
        s.add("  socket_close(\"" + SOCKET + "\")");
        s.add("else:");
        s.add("  popup(\"RealSense Pick: no pick server at " + host + ":" + port
                + " - start the cockpit there (perception gui --bind <its address>) and allow it through the"
                + " firewall\", \"RealSense Pick\", False, True, blocking=True)");
        s.add("end");
        s.add("set_tcp(rs_tcp0)");
        return s;
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
