package com.olympuscontrols.realsensepilot;

import com.ur.urcap.api.contribution.ProgramNodeContribution;
import com.ur.urcap.api.contribution.program.CreationContext;
import com.ur.urcap.api.contribution.program.ProgramAPIProvider;
import com.ur.urcap.api.domain.data.DataModel;
import com.ur.urcap.api.domain.program.nodes.builtin.CommentNode;
import com.ur.urcap.api.domain.program.structure.TreeNode;
import com.ur.urcap.api.domain.script.ScriptWriter;
import com.ur.urcap.api.domain.undoredo.UndoableChanges;
import com.ur.urcap.api.domain.userinteraction.RobotPositionCallback2;
import com.ur.urcap.api.domain.userinteraction.robot.movement.MovementCancelEvent;
import com.ur.urcap.api.domain.userinteraction.robot.movement.MovementCompleteEvent;
import com.ur.urcap.api.domain.userinteraction.robot.movement.MovementErrorEvent;
import com.ur.urcap.api.domain.userinteraction.robot.movement.RobotMovementCallback;
import com.ur.urcap.api.domain.value.Pose;
import com.ur.urcap.api.domain.value.jointposition.JointPosition;
import com.ur.urcap.api.domain.value.jointposition.JointPositions;
import com.ur.urcap.api.domain.value.robotposition.PositionParameters;
import com.ur.urcap.api.domain.value.simple.Angle;
import com.ur.urcap.api.domain.value.simple.Length;
import com.ur.urcap.api.domain.variable.Variable;
import java.io.IOException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadFactory;
import javax.swing.SwingUtilities;

/**
 * One RealSense Pick node in a program. Teach time (the node's screen open): the
 * cockpit's colour feed with the blocks its detector sees, a tap to choose one, an
 * optional survey position taught through PolyScope's own screen (without one the first
 * look is from wherever the arm is), and two checks that open
 * PolyScope's hold-to-move screen over and into the grasp the program would make —
 * all in Local mode. The part's rough size (optional) filters what the detector offers,
 * on this screen and when the program runs. Run time: {@link PickScript}.
 */
public class PickContribution implements ProgramNodeContribution {
    static final String KEY_SURVEY = "surveyJoints";
    static final String KEY_TAP_U = "tapU";
    static final String KEY_TAP_V = "tapV";
    static final String KEY_GRIP_MM = "gripBelowTopMm";
    static final String KEY_LIFT_MM = "liftMm";
    static final String KEY_PORT = "pickPort";
    static final String KEY_PART_L = "partLengthMm";
    static final String KEY_PART_W = "partWidthMm";
    static final String KEY_PART_H = "partHeightMm";
    static final String KEY_PART_TOL = "partTolPct";
    static final String KEY_VARIABLE = "foundVariable";
    static final String KEY_TEMPLATED = "templated";
    static final String VARIABLE_NAME = "rs_pick_found";
    static final String GRIP_HERE = "Close the gripper here: insert your gripper's Close node";
    private static final int POLL_TIMEOUT_MS = 1500;
    private static final long DETECT_EVERY_MS = 1000;

    private final ProgramAPIProvider api;
    private final PickView view;
    private final DataModel model;
    private final ExecutorService actions = Executors.newSingleThreadExecutor(daemon("realsense-pick-actions"));

    private volatile boolean open;
    private volatile Thread poller;
    private volatile long seq;
    private volatile long detectedAt;
    private volatile List<int[]> blocks = new ArrayList<int[]>();
    private volatile List<PickView.Reject> rejected = new ArrayList<PickView.Reject>();

    PickContribution(ProgramAPIProvider api, PickView view, DataModel model, CreationContext context) {
        this.api = api;
        this.view = view;
        this.model = model;
    }

    // -- where things come from ---------------------------------------------------------------

    /** The cockpit is the Installation node's: one address per robot, not per node. */
    Cockpit cockpit() {
        PilotContribution installation = api.getProgramAPI().getInstallationNode(PilotContribution.class);
        return new Cockpit(installation == null ? "" : installation.savedUrl());
    }

    double[] surveyJoints() {
        JointPositions q = model.get(KEY_SURVEY, (JointPositions) null);
        if (q == null) return null;
        JointPosition[] all = q.getAllJointPositions();
        double[] out = new double[all.length];
        for (int i = 0; i < all.length; i++) out[i] = all[i].getPosition(Angle.Unit.RAD);
        return out;
    }

    int tapU() {
        return model.get(KEY_TAP_U, -1);
    }

    int tapV() {
        return model.get(KEY_TAP_V, -1);
    }

    double gripMm() {
        return model.get(KEY_GRIP_MM, 15.0);
    }

    double liftMm() {
        return model.get(KEY_LIFT_MM, 60.0);
    }

    double partLengthMm() {
        return model.get(KEY_PART_L, 0.0);
    }

    double partWidthMm() {
        return model.get(KEY_PART_W, 0.0);
    }

    double partHeightMm() {
        return model.get(KEY_PART_H, 0.0);
    }

    double partTolPct() {
        return model.get(KEY_PART_TOL, 25.0);
    }

    PickScript script() {
        PickScript s = new PickScript();
        s.host = PickScript.hostOf(cockpit().base);
        s.port = model.get(KEY_PORT, PickScript.DEFAULT_PICK_PORT);
        s.surveyJoints = surveyJoints();
        s.tapU = tapU();
        s.tapV = tapV();
        s.gripBelowTopMm = gripMm();
        s.liftMm = liftMm();
        s.partLengthMm = partLengthMm();
        s.partWidthMm = partWidthMm();
        s.partHeightMm = partHeightMm();
        s.partTolPct = partTolPct();
        return s;
    }

    private TreeNode tree() {
        return api.getProgramAPI().getProgramModel().getRootTreeNode(this);
    }

    /** A child that is not a Comment: the operator's gripper node is in. */
    boolean hasGripChild() {
        try {
            for (TreeNode child : tree().getChildren()) {
                if (!(child.getProgramNode() instanceof CommentNode)) return true;
            }
        } catch (RuntimeException e) {
            return false;
        }
        return false;
    }

    // -- program node --------------------------------------------------------------------------

    @Override
    public void openView() {
        open = true;
        ensureTemplate();
        view.show(this);
        view.setStatus("connecting to " + cockpit().base + "…", PilotView.Kind.INFO);
        startPolling();
    }

    @Override
    public void closeView() {
        open = false;
        Thread t = poller;
        if (t != null) t.interrupt();
    }

    @Override
    public String getTitle() {
        PickScript s = script();
        String which = tapU() >= 0 ? "tapped" : "nearest";
        return s.hasPart() ? "RealSense Pick (" + s.partText() + ", " + which + ")"
                : "RealSense Pick (" + which + " block)";
    }

    @Override
    public boolean isDefined() {
        return script().problem() == null && hasGripChild();
    }

    @Override
    public void generateScript(ScriptWriter writer) {
        PickScript s = script();
        Variable v = model.get(KEY_VARIABLE, (Variable) null);
        if (v != null) s.foundVariable = writer.getResolvedVariableName(v);
        for (String line : s.beforeChildren()) writer.appendLine(line);
        writer.writeChildren();
        for (String line : s.afterChildren()) writer.appendLine(line);
    }

    /** What still stops the node from running, for the screen; null when ready. */
    String problem() {
        String p = script().problem();
        if (p != null) return p;
        if (!hasGripChild()) return "insert your gripper's Close node inside this node (it runs at the grip)";
        return null;
    }

    // -- first open: the comment child and the result variable -----------------------------------

    private void ensureTemplate() {
        if (model.get(KEY_TEMPLATED, false)) return;
        change(new UndoableChanges() {
            @Override
            public void executeChanges() {
                model.set(KEY_TEMPLATED, true);
                try {
                    TreeNode root = tree();
                    if (root.getChildren().isEmpty()) {
                        CommentNode c = api.getProgramAPI().getProgramModel().getProgramNodeFactory().createCommentNode();
                        c.setComment(GRIP_HERE);
                        root.addChild(c);
                    }
                } catch (Exception e) {
                    // an empty node is still usable: the screen says what to insert
                }
                try {
                    if (model.get(KEY_VARIABLE, (Variable) null) == null) {
                        model.set(KEY_VARIABLE, api.getProgramAPI().getVariableModel().getVariableFactory()
                                .createGlobalVariable(VARIABLE_NAME));
                    }
                } catch (Exception e) {
                    // the script falls back to a plain rs_pick_found
                }
            }
        });
    }

    private void change(UndoableChanges changes) {
        api.getProgramAPI().getUndoRedoManager().recordChanges(changes);
    }

    // -- teach: survey position ---------------------------------------------------------------

    void setSurvey() {
        api.getUserInterfaceAPI().getUserInteraction().getUserDefinedRobotPosition(new RobotPositionCallback2() {
            @Override
            public void onOk(final PositionParameters position) {
                change(new UndoableChanges() {
                    @Override
                    public void executeChanges() {
                        model.set(KEY_SURVEY, position.getJointPositions());
                    }
                });
                view.show(PickContribution.this);
                view.setStatus("survey position taught — the camera must see the blocks from here, ≥ 0.25 m away",
                        PilotView.Kind.OK);
            }
        });
    }

    void clearSurvey() {
        change(new UndoableChanges() {
            @Override
            public void executeChanges() {
                model.remove(KEY_SURVEY);
            }
        });
        view.show(this);
        view.setStatus("no survey position: the first look is from wherever the arm is when the node runs",
                PilotView.Kind.OK);
    }

    void moveToSurvey() {
        JointPositions q = model.get(KEY_SURVEY, (JointPositions) null);
        if (q == null) {
            view.setStatus("no survey position taught: the node looks from where the arm is", PilotView.Kind.WARN);
            return;
        }
        api.getUserInterfaceAPI().getUserInteraction().getRobotMovement().requestUserToMoveRobot(q, done("survey position"));
    }

    // -- teach: which block ---------------------------------------------------------------------

    void onTap(final int x, final int y) {
        change(new UndoableChanges() {
            @Override
            public void executeChanges() {
                model.set(KEY_TAP_U, x);
                model.set(KEY_TAP_V, y);
            }
        });
        int[] near = nearestBlock(x, y);
        view.show(this);
        PickView.Reject off = near == null ? nearestReject(x, y) : null;
        if (off != null) {
            view.setStatus("tapped (" + x + ", " + y + ") — that one measures " + off.label + ", not the part size "
                    + script().partText() + "; the program picks the part nearest this spot", PilotView.Kind.WARN);
        } else if (near == null) {
            view.setStatus("tapped (" + x + ", " + y + ") — no block detected there yet; the program picks the block "
                    + "nearest this spot", PilotView.Kind.WARN);
        } else {
            view.setStatus("the program will pick the block nearest (" + x + ", " + y + ") — " + near[2] + " × "
                    + near[3] + " mm now", PilotView.Kind.OK);
        }
    }

    void anyBlock() {
        change(new UndoableChanges() {
            @Override
            public void executeChanges() {
                model.set(KEY_TAP_U, -1);
                model.set(KEY_TAP_V, -1);
            }
        });
        view.show(this);
        view.setStatus("the program will pick the block nearest the middle of the picture", PilotView.Kind.OK);
    }

    void setGripMm(final double mm) {
        change(new UndoableChanges() {
            @Override
            public void executeChanges() {
                model.set(KEY_GRIP_MM, mm);
            }
        });
        view.show(this);
    }

    void setLiftMm(final double mm) {
        change(new UndoableChanges() {
            @Override
            public void executeChanges() {
                model.set(KEY_LIFT_MM, mm);
            }
        });
        view.show(this);
    }

    // -- teach: the part's rough size ------------------------------------------------------------

    /** {@code key}: one of the KEY_PART_* keys; the value is clamped to what the script accepts. */
    void setPart(final String key, final double value) {
        change(new UndoableChanges() {
            @Override
            public void executeChanges() {
                model.set(key, value);
            }
        });
        detectedAt = 0; // redraw the feed's candidates under the new size now, not in a second
        view.show(this);
        String p = script().problem();
        view.setStatus(p == null ? "the program looks only for a part " + script().partText()
                : "part size: " + p, p == null ? PilotView.Kind.OK : PilotView.Kind.WARN);
    }

    void anySize() {
        change(new UndoableChanges() {
            @Override
            public void executeChanges() {
                model.remove(KEY_PART_L);
                model.remove(KEY_PART_W);
                model.remove(KEY_PART_H);
            }
        });
        detectedAt = 0;
        view.show(this);
        view.setStatus("no part size: the program takes any block-sized white object", PilotView.Kind.OK);
    }

    /** {@code ?part=60x40x30&tol=25} for the cockpit's routes, or "" when no size is set. */
    private String partQuery() {
        PickScript s = script();
        if (!s.hasPart()) return "";
        String t = s.partToken().trim(); // "part=60x40x30 tol=25"
        return "?" + t.replace(' ', '&');
    }

    private PickView.Reject nearestReject(int x, int y) {
        for (PickView.Reject r : rejected) {
            if ((long) (r.u - x) * (r.u - x) + (long) (r.v - y) * (r.v - y) <= 80L * 80L) return r;
        }
        return null;
    }

    private int[] nearestBlock(int x, int y) {
        int[] best = null;
        long bestD = Long.MAX_VALUE;
        for (int[] b : blocks) {
            long d = (long) (b[0] - x) * (b[0] - x) + (long) (b[1] - y) * (b[1] - y);
            if (d < bestD) {
                bestD = d;
                best = b;
            }
        }
        return best != null && bestD <= 80L * 80L ? best : null;
    }

    // -- teach: check the grasp with PolyScope's hold-to-move screen ------------------------------

    /** {@code which}: "hover" (the fingertips 40 mm over the top) or "grip" (the grip depth). */
    void check(final String which) {
        view.setStatus("asking the cockpit where the " + which + " would be from here…", PilotView.Kind.INFO);
        final Cockpit c = cockpit();
        actions.submit(new Runnable() {
            @Override
            public void run() {
                try {
                    Map<String, Object> body = new LinkedHashMap<String, Object>();
                    body.put("u", tapU());
                    body.put("v", tapV());
                    body.put("grip_below_mm", gripMm());
                    PickScript s = script();
                    if (s.hasPart()) {
                        String t = s.partToken().trim(); // "part=60x40x30 tol=25"
                        body.put("part", t.substring("part=".length(), t.indexOf(' ')));
                        body.put("tol", s.partTolPct);
                    }
                    Map<String, Object> res = c.post("/api/pick/preview", body, 15000);
                    if (!Boolean.TRUE.equals(res.get("ok"))) {
                        Object e = res.get("error");
                        view.setStatus("no " + which + " to check: " + (e == null ? "?" : e), PilotView.Kind.WARN);
                        return;
                    }
                    final double[] p = Cockpit.six(res.get("polyscope_" + which + "_pose"));
                    if (p == null) {
                        Object note = res.get("polyscope_note");
                        view.setStatus("PolyScope can't be given the " + which + ": " + (note == null ? "?" : note),
                                PilotView.Kind.ERR);
                        return;
                    }
                    SwingUtilities.invokeLater(new Runnable() {
                        @Override
                        public void run() {
                            Pose pose = api.getProgramAPI().getValueFactoryProvider().getPoseFactory()
                                    .createPose(p[0], p[1], p[2], p[3], p[4], p[5], Length.Unit.M, Angle.Unit.RAD);
                            view.setStatus("PolyScope's move screen is open — hold Move to go to the " + which
                                    + "; let go to stop", PilotView.Kind.OK);
                            api.getUserInterfaceAPI().getUserInteraction().getRobotMovement()
                                    .requestUserToMoveRobot(pose, done(which));
                        }
                    });
                } catch (IOException e) {
                    view.setStatus(Cockpit.explain(e, c.base), PilotView.Kind.ERR);
                } catch (RuntimeException e) {
                    view.setStatus("check: " + e.getMessage(), PilotView.Kind.ERR);
                }
            }
        });
    }

    private RobotMovementCallback done(final String what) {
        return new RobotMovementCallback() {
            @Override
            public void onComplete(MovementCompleteEvent event) {
                view.setStatus("at the " + what, PilotView.Kind.OK);
            }

            @Override
            public void onCancel(MovementCancelEvent event) {
                view.setStatus("move to the " + what + " cancelled", PilotView.Kind.WARN);
            }

            @Override
            public void onError(MovementErrorEvent event) {
                view.setStatus("move to the " + what + ": " + event.getErrorType(), PilotView.Kind.ERR);
            }
        };
    }

    // -- the feed and the detector's blocks -----------------------------------------------------

    private synchronized void startPolling() {
        if (poller != null && poller.isAlive()) return;
        Thread t = new Thread(new Runnable() {
            @Override
            public void run() {
                pollLoop();
            }
        }, "realsense-pick-feed");
        t.setDaemon(true);
        poller = t;
        t.start();
    }

    private void pollLoop() {
        boolean announced = false;
        while (open && !Thread.currentThread().isInterrupted()) {
            Cockpit c = cockpit();
            try {
                Cockpit.Frame f = c.colorPng(seq, POLL_TIMEOUT_MS);
                if (f.status != 200) {
                    announced = false;
                    view.setLive(false, null);
                    view.setStatus(f.status == 503 ? "the cockpit is up but has no frame yet (camera opening?)"
                            : "the cockpit at " + c.base + " answered HTTP " + f.status, PilotView.Kind.WARN);
                    sleep(1000);
                    continue;
                }
                seq = f.seq;
                view.setFrame(f.image);
                view.setLive(true, f.fps);
                if (System.currentTimeMillis() - detectedAt > DETECT_EVERY_MS) {
                    detectedAt = System.currentTimeMillis();
                    detect(c);
                }
                if (!announced) {
                    String p = problem();
                    view.setStatus(p == null ? "ready — live from " + c.base : "live from " + c.base + " · to do: " + p,
                            p == null ? PilotView.Kind.OK : PilotView.Kind.INFO);
                    announced = true;
                }
            } catch (IOException e) {
                announced = false;
                view.setLive(false, null);
                view.setStatus(Cockpit.explain(e, c.base), PilotView.Kind.ERR);
                sleep(1500);
            } catch (RuntimeException e) {
                announced = false;
                view.setLive(false, null);
                view.setStatus(Cockpit.explain(e, c.base), PilotView.Kind.ERR);
                sleep(1500);
            }
        }
    }

    private void detect(Cockpit c) throws IOException {
        Map<String, Object> res = c.get("/api/pick/detect" + partQuery(), 3000);
        List<int[]> found = new ArrayList<int[]>();
        Object list = res.get("blocks");
        if (list instanceof List) {
            for (Object o : (List<?>) list) {
                if (!(o instanceof Map)) continue;
                Map<?, ?> b = (Map<?, ?>) o;
                int[] px = ints(b.get("pixel"), 2);
                int[] mm = ints(b.get("size_mm"), 2);
                if (px != null) found.add(new int[] {px[0], px[1], mm == null ? 0 : mm[0], mm == null ? 0 : mm[1]});
            }
        }
        blocks = found;
        List<PickView.Reject> off = new ArrayList<PickView.Reject>();
        Object rej = res.get("rejected");
        if (rej instanceof List) {
            for (Object o : (List<?>) rej) {
                if (!(o instanceof Map)) continue;
                Map<?, ?> r = (Map<?, ?>) o;
                int[] px = ints(r.get("pixel"), 2);
                int[] mm = ints(r.get("size_mm"), 2);
                Object why = r.get("why");
                if (px == null) continue;
                String label = (mm == null ? "" : mm[0] + "×" + mm[1] + " mm ") + (why instanceof String ? why : "");
                off.add(new PickView.Reject(px[0], px[1], label.trim()));
            }
        }
        rejected = off;
        if (res.get("pick_port") instanceof Number) {
            final int port = ((Number) res.get("pick_port")).intValue();
            if (port != model.get(KEY_PORT, PickScript.DEFAULT_PICK_PORT) && port > 0) {
                SwingUtilities.invokeLater(new Runnable() {
                    @Override
                    public void run() {
                        change(new UndoableChanges() {
                            @Override
                            public void executeChanges() {
                                model.set(KEY_PORT, port);
                            }
                        });
                    }
                });
            }
        }
        view.setBlocks(found, off, tapU(), tapV(), script().hasPart() ? script().partText() : null);
    }

    private static int[] ints(Object xs, int n) {
        if (!(xs instanceof List) || ((List<?>) xs).size() != n) return null;
        int[] out = new int[n];
        for (int i = 0; i < n; i++) {
            Object v = ((List<?>) xs).get(i);
            if (!(v instanceof Number)) return null;
            out[i] = ((Number) v).intValue();
        }
        return out;
    }

    private static void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    private static ThreadFactory daemon(final String name) {
        return new ThreadFactory() {
            @Override
            public Thread newThread(Runnable r) {
                Thread t = new Thread(r, name);
                t.setDaemon(true);
                return t;
            }
        };
    }
}
