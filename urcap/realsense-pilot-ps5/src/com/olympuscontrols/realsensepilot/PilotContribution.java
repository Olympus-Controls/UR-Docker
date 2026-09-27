package com.olympuscontrols.realsensepilot;

import com.ur.urcap.api.contribution.InstallationNodeContribution;
import com.ur.urcap.api.contribution.installation.InstallationAPIProvider;
import com.ur.urcap.api.domain.data.DataModel;
import com.ur.urcap.api.domain.script.ScriptWriter;
import com.ur.urcap.api.domain.userinteraction.robot.movement.MovementCompleteEvent;
import com.ur.urcap.api.domain.userinteraction.robot.movement.MovementErrorEvent;
import com.ur.urcap.api.domain.userinteraction.robot.movement.RobotMovement;
import com.ur.urcap.api.domain.userinteraction.robot.movement.RobotMovementCallback;
import com.ur.urcap.api.domain.value.Pose;
import com.ur.urcap.api.domain.value.ValueFactoryProvider;
import com.ur.urcap.api.domain.value.jointposition.JointPositions;
import com.ur.urcap.api.domain.value.simple.Angle;
import com.ur.urcap.api.domain.value.simple.Length;
import java.io.IOException;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadFactory;
import javax.swing.SwingUtilities;

/**
 * The node's behaviour — the PolyScope X presenter's, on PolyScope 5: poll the cockpit's
 * colour feed while the node is open, hover → depth, tap → segment → locate, and the same
 * five buttons. Network calls run off the Swing thread; results come back through
 * {@link PilotView}'s setters, which marshal onto it.
 */
public class PilotContribution implements InstallationNodeContribution {
    static final String KEY_COCKPIT_URL = "cockpitUrl";
    private static final int POLL_TIMEOUT_MS = 1500;
    private static final int HOVER_MS = 150;

    private final InstallationAPIProvider api;
    private final PilotView view;
    private final DataModel model;
    private final ExecutorService actions = Executors.newSingleThreadExecutor(daemon("realsense-pilot-actions"));

    private volatile Cockpit cockpit;
    private volatile boolean open;
    private volatile Thread poller;
    private volatile long seq;
    private volatile Map<String, Object> located;
    private volatile int[] hoverPending;
    private volatile Thread hoverThread;

    PilotContribution(InstallationAPIProvider api, PilotView view, DataModel model) {
        this.api = api;
        this.view = view;
        this.model = model;
        this.cockpit = new Cockpit(savedUrl());
    }

    String savedUrl() {
        return model.get(KEY_COCKPIT_URL, "");
    }

    String cockpitBase() {
        return cockpit.base;
    }

    // -- lifecycle --------------------------------------------------------------------------

    @Override
    public void openView() {
        open = true;
        view.showUrl(savedUrl());
        view.setStatus("connecting to " + cockpit.base + "…", PilotView.Kind.INFO);
        startPolling();
    }

    @Override
    public void closeView() {
        open = false;
        Thread t = poller;
        if (t != null) t.interrupt();
        Thread h = hoverThread;
        if (h != null) h.interrupt();
    }

    @Override
    public void generateScript(ScriptWriter writer) {
        // the node adds nothing to programs: it is an operator tool, not a program step
    }

    // -- cockpit URL --------------------------------------------------------------------------

    void saveUrl(String value) {
        String v = value == null ? "" : value.trim();
        model.set(KEY_COCKPIT_URL, v);
        cockpit = new Cockpit(v);
        seq = 0;
        view.setStatus("cockpit: " + cockpit.base, PilotView.Kind.INFO);
    }

    // -- the feed ----------------------------------------------------------------------------

    private synchronized void startPolling() {
        if (poller != null && poller.isAlive()) return;
        Thread t = new Thread(new Runnable() {
            @Override
            public void run() {
                pollLoop();
            }
        }, "realsense-pilot-feed");
        t.setDaemon(true);
        poller = t;
        t.start();
    }

    private void pollLoop() {
        boolean announced = false;
        while (open && !Thread.currentThread().isInterrupted()) {
            Cockpit c = cockpit;
            try {
                Cockpit.Frame f = c.colorPng(seq, POLL_TIMEOUT_MS);
                if (f.status == 503) {
                    view.setLive(false, null);
                    view.setStatus("the cockpit is up but has no frame yet (camera opening?)", PilotView.Kind.WARN);
                    sleep(500);
                    continue;
                }
                if (f.status != 200) {
                    announced = false;
                    view.setLive(false, null);
                    view.setStatus("the cockpit at " + c.base + " answered HTTP " + f.status + " on /api/color.png"
                            + (f.status == 404 ? " — it predates the URCap routes; update it and restart." : "."),
                            PilotView.Kind.ERR);
                    sleep(1500);
                    continue;
                }
                seq = f.seq;
                view.setFrame(f.image);
                view.setLive(true, f.fps);
                if (!announced) {
                    view.setStatus("live from " + c.base, PilotView.Kind.OK);
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

    private static void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    // -- pixels ------------------------------------------------------------------------------

    void onHover(int x, int y) {
        hoverPending = new int[] {x, y};
        Thread h = hoverThread;
        if (h != null && h.isAlive()) return;
        Thread t = new Thread(new Runnable() {
            @Override
            public void run() {
                sleep(HOVER_MS);
                int[] p = hoverPending;
                if (p == null || !open) return;
                try {
                    Map<String, Object> res = cockpit.get("/api/point?x=" + p[0] + "&y=" + p[1], 3000);
                    java.util.List<?> pt = res.get("point_m") instanceof java.util.List ? (java.util.List<?>) res.get("point_m") : null;
                    if (Boolean.TRUE.equals(res.get("ok")) && pt != null && pt.size() == 3) {
                        view.setHover("(" + p[0] + ", " + p[1] + ") → " + Cockpit.fmtVec(pt) + " m in the camera · "
                                + Cockpit.fmt(pt.get(2), 2) + " m away");
                    } else {
                        view.setHover("(" + p[0] + ", " + p[1] + ") → no depth");
                    }
                } catch (IOException e) {
                    // the feed loop reports connectivity
                }
            }
        }, "realsense-pilot-hover");
        t.setDaemon(true);
        hoverThread = t;
        t.start();
    }

    void onClick(final int x, final int y) {
        located = null;
        view.setMoveEnabled(false, false);
        view.setTarget("");
        view.setStatus("segmenting at (" + x + ", " + y + ")…", PilotView.Kind.INFO);
        actions.submit(new Runnable() {
            @Override
            public void run() {
                try {
                    Map<String, Object> body = new LinkedHashMap<String, Object>();
                    body.put("x", x);
                    body.put("y", y);
                    Map<String, Object> seg = cockpit.post("/api/segment", body, 15000);
                    if (!Boolean.TRUE.equals(seg.get("ok"))) throw new IllegalStateException(errorOf(seg, "segment failed"));
                    Object features = seg.get("features");
                    Object point = features instanceof Map ? ((Map<?, ?>) features).get("point_m") : null;
                    if (point == null) {
                        throw new IllegalStateException("the segment has no depth point — tap the object, not the void");
                    }
                    Map<String, Object> req = new LinkedHashMap<String, Object>();
                    req.put("point_m", point);
                    Map<String, Object> loc = cockpit.post("/api/robot/locate", req, 15000);
                    if (!Boolean.TRUE.equals(loc.get("ok"))) {
                        throw new IllegalStateException(errorOf(loc, "locate failed (is the cockpit's robot link up?)"));
                    }
                    located = loc;
                    view.setTarget(Cockpit.targetText(loc));
                    boolean offer = !Boolean.FALSE.equals(loc.get("reachable"));
                    view.setMoveEnabled(offer, offer);
                    view.setStatus(offer ? "target located — choose how to move"
                            : "target located but out of reach — pick a nearer point",
                            offer ? PilotView.Kind.OK : PilotView.Kind.WARN);
                } catch (IOException e) {
                    view.setStatus(Cockpit.explain(e, cockpit.base), PilotView.Kind.ERR);
                } catch (RuntimeException e) {
                    view.setStatus(String.valueOf(e.getMessage()), PilotView.Kind.ERR);
                }
            }
        });
    }

    private static String errorOf(Map<String, Object> res, String fallback) {
        Object e = res.get("error");
        return e == null ? fallback : e.toString();
    }

    // -- moving ------------------------------------------------------------------------------

    /**
     * PolyScope's own hold-to-move screen. The target is the controller's joint solution
     * for the flange target (``joint_target`` from locate: get_inverse_kin with the TCP
     * forced to the flange), so PolyScope never re-solves the pose through an active TCP
     * the cockpit may disagree about. Without one (no IK answer) the approach pose goes to
     * PolyScope for its active TCP.
     */
    void movePolyScope() {
        final Map<String, Object> loc = located;
        if (loc == null) return;
        final RobotMovement movement = api.getUserInterfaceAPI().getUserInteraction().getRobotMovement();
        final ValueFactoryProvider values = api.getInstallationAPI().getValueFactoryProvider();
        final double[] q = Cockpit.six(loc.get("joint_target"));
        final double[] p = Cockpit.six(loc.get("approach_pose"));
        if (q == null && p == null) {
            view.setStatus("the located target has neither a joint solution nor an approach pose", PilotView.Kind.ERR);
            return;
        }
        final RobotMovementCallback done = new RobotMovementCallback() {
            @Override
            public void onComplete(MovementCompleteEvent event) {
                view.setStatus("PolyScope move finished", PilotView.Kind.OK);
            }

            @Override
            public void onCancel(com.ur.urcap.api.domain.userinteraction.robot.movement.MovementCancelEvent event) {
                view.setStatus("PolyScope move cancelled", PilotView.Kind.WARN);
            }

            @Override
            public void onError(MovementErrorEvent event) {
                view.setStatus("PolyScope move: " + event.getErrorType(), PilotView.Kind.ERR);
            }
        };
        SwingUtilities.invokeLater(new Runnable() {
            @Override
            public void run() {
                try {
                    if (q != null) {
                        JointPositions joints = values.getJointPositionFactory()
                                .createJointPositions(q[0], q[1], q[2], q[3], q[4], q[5], Angle.Unit.RAD);
                        view.setStatus("PolyScope's move screen is open — hold Move to go to the joint solution",
                                PilotView.Kind.OK);
                        movement.requestUserToMoveRobot(joints, done);
                    } else {
                        Pose pose = values.getPoseFactory()
                                .createPose(p[0], p[1], p[2], p[3], p[4], p[5], Length.Unit.M, Angle.Unit.RAD);
                        view.setStatus("PolyScope's move screen is open — hold Move (pose for the active TCP)",
                                PilotView.Kind.OK);
                        movement.requestUserToMoveRobot(pose, done);
                    }
                } catch (RuntimeException e) {
                    view.setStatus("PolyScope move: " + e.getMessage(), PilotView.Kind.ERR);
                }
            }
        });
    }

    void moveCockpit() {
        final Map<String, Object> loc = located;
        if (loc == null) return;
        view.setMoveEnabled(true, false);
        view.setStatus("moving through the cockpit (Primary, safety-enveloped)…", PilotView.Kind.INFO);
        actions.submit(new Runnable() {
            @Override
            public void run() {
                try {
                    Map<String, Object> body = new LinkedHashMap<String, Object>();
                    body.put("pose", loc.get("approach_pose"));
                    body.put("velocity", 0.1);
                    Map<String, Object> res = cockpit.post("/api/robot/move", body, 60000);
                    if (Boolean.TRUE.equals(res.get("ok"))) {
                        view.setStatus("landed at " + Cockpit.fmtVec(res.get("landed")), PilotView.Kind.OK);
                    } else {
                        view.setStatus("move refused: " + refusal(res), PilotView.Kind.ERR);
                    }
                } catch (IOException e) {
                    view.setStatus("move: " + Cockpit.explain(e, cockpit.base), PilotView.Kind.ERR);
                } finally {
                    view.setMoveEnabled(located != null, located != null);
                }
            }
        });
    }

    private static String refusal(Map<String, Object> res) {
        if (res.get("error") != null) return res.get("error").toString();
        Object safety = res.get("safety");
        if (safety instanceof Map && ((Map<?, ?>) safety).get("violations") != null) {
            return ((Map<?, ?>) safety).get("violations").toString();
        }
        if (Boolean.TRUE.equals(res.get("protective_stop"))) return "protective stop — Bring up, then retry";
        return "the move did not confirm";
    }

    void bringUp() {
        view.setStatus("bringing the robot up…", PilotView.Kind.INFO);
        actions.submit(new Runnable() {
            @Override
            public void run() {
                try {
                    Map<String, Object> res = cockpit.post("/api/robot/bring_up", new LinkedHashMap<String, Object>(), 180000);
                    boolean ok = Boolean.TRUE.equals(res.get("ok"));
                    view.setStatus(ok ? "robot " + orElse(res.get("robot_mode"), "up") + " / " + orElse(res.get("safety_mode"), "")
                            : "bring-up failed: " + orElse(res.get("error"), "?"), ok ? PilotView.Kind.OK : PilotView.Kind.ERR);
                } catch (IOException e) {
                    view.setStatus("bring-up: " + Cockpit.explain(e, cockpit.base), PilotView.Kind.ERR);
                }
            }
        });
    }

    /** STOP skips the action queue: it must not wait behind a move that is still running. */
    void stop() {
        Thread t = new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    Map<String, Object> res = cockpit.post("/api/robot/stop", new LinkedHashMap<String, Object>(), 10000);
                    boolean ok = Boolean.TRUE.equals(res.get("ok"));
                    view.setStatus(ok ? "stopped" : "stop failed: " + orElse(res.get("error"), "?"),
                            ok ? PilotView.Kind.WARN : PilotView.Kind.ERR);
                } catch (IOException e) {
                    view.setStatus("stop: " + Cockpit.explain(e, cockpit.base), PilotView.Kind.ERR);
                }
            }
        }, "realsense-pilot-stop");
        t.setDaemon(true);
        t.start();
    }

    void clear() {
        located = null;
        view.clearMark();
        view.setTarget("");
        view.setMoveEnabled(false, false);
        view.setStatus("cleared", PilotView.Kind.INFO);
    }

    private static String orElse(Object v, String fallback) {
        return v == null ? fallback : v.toString();
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
