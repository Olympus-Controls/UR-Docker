package com.olympuscontrols.realsensepilot;

import com.ur.urcap.api.contribution.ContributionProvider;
import com.ur.urcap.api.contribution.ViewAPIProvider;
import com.ur.urcap.api.contribution.program.swing.SwingProgramNodeView;
import com.ur.urcap.api.domain.userinteraction.keyboard.KeyboardInputCallback;
import com.ur.urcap.api.domain.userinteraction.keyboard.KeyboardNumberInput;
import java.awt.BasicStroke;
import java.awt.Color;
import java.awt.Component;
import java.awt.Dimension;
import java.awt.FlowLayout;
import java.awt.Font;
import java.awt.Graphics;
import java.awt.Graphics2D;
import java.awt.Insets;
import java.awt.Rectangle;
import java.awt.RenderingHints;
import java.awt.event.ActionEvent;
import java.awt.event.ActionListener;
import java.awt.event.MouseAdapter;
import java.awt.event.MouseEvent;
import java.awt.image.BufferedImage;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import javax.swing.BorderFactory;
import javax.swing.Box;
import javax.swing.BoxLayout;
import javax.swing.JButton;
import javax.swing.JComponent;
import javax.swing.JLabel;
import javax.swing.JPanel;
import javax.swing.JTextArea;
import javax.swing.SwingUtilities;

/**
 * The RealSense Pick node's screen: the feed with the blocks the cockpit's detector
 * sees (tap one to choose it), the survey position, grip depth and lift, the two
 * checks, and what the node still needs. One view serves every Pick node in the
 * program, so actions go to {@code provider.get()} — the node that is open.
 */
public class PickView implements SwingProgramNodeView<PickContribution> {
    private static final Color INK = new Color(0x1f2a37);
    private static final Color MUTED = new Color(0x5b6b7d);
    private static final Color STATUS_BG = new Color(0xeef2f7);
    private static final Color OK_BG = new Color(0xe3f5ea);
    private static final Color WARN_BG = new Color(0xfff4d6);
    private static final Color ERR_BG = new Color(0xfde2e2);
    private static final Color LIVE = new Color(0x1d9a5a);
    private static final Color DEAD = new Color(0xd64545);
    private static final Color IDLE = new Color(0xc0c8d2);
    private static final Color PRIMARY = new Color(0x1f5fbf);
    private static final Color STAGE = new Color(0x0f1620);
    private static final Color MARK = new Color(0xffd23f);
    private static final Color BLOCK = new Color(0x4fd18b);

    private final ViewAPIProvider api;
    private final JLabel dot = new JLabel("●");
    private final JLabel fps = new JLabel("");
    private final JLabel cockpit = new JLabel("");
    private final JLabel survey = new JLabel("");
    private final JLabel choice = new JLabel("");
    private final Feed feed = new Feed();
    private final JTextArea status = new JTextArea();
    private final JButton grip = button("Grip depth", null, null);
    private final JButton lift = button("Lift", null, null);
    private ContributionProvider<PickContribution> provider;

    PickView(ViewAPIProvider api) {
        this.api = api;
    }

    @Override
    public void buildUI(JPanel host, ContributionProvider<PickContribution> contributionProvider) {
        this.provider = contributionProvider;
        // PolyScope's own panel refuses most setters (the Pilot node found setBorder throws): add one we own
        host.setLayout(new BoxLayout(host, BoxLayout.Y_AXIS));
        JPanel panel = new JPanel();
        panel.setLayout(new BoxLayout(panel, BoxLayout.Y_AXIS));
        panel.setBorder(BorderFactory.createEmptyBorder(8, 12, 8, 12));
        panel.setAlignmentX(Component.LEFT_ALIGNMENT);
        host.add(panel);

        JPanel title = row();
        dot.setForeground(IDLE);
        dot.setFont(dot.getFont().deriveFont(16f));
        JLabel name = new JLabel("RealSense Pick");
        name.setFont(name.getFont().deriveFont(Font.BOLD, 20f));
        name.setForeground(INK);
        JLabel version = new JLabel("v" + PickScript.VERSION);
        version.setForeground(MUTED);
        fps.setForeground(MUTED);
        title.add(dot);
        title.add(name);
        title.add(version);
        title.add(fps);
        panel.add(title);
        cockpit.setForeground(MUTED);
        cockpit.setAlignmentX(Component.LEFT_ALIGNMENT);
        panel.add(cockpit);

        feed.setAlignmentX(Component.LEFT_ALIGNMENT);
        panel.add(feed);
        panel.add(Box.createVerticalStrut(4));

        JPanel which = row();
        choice.setForeground(INK);
        JButton any = button("Any block", null, null);
        any.setToolTipText("pick the block nearest the middle of the picture instead of a tapped one");
        on(any, new Runnable() {
            @Override
            public void run() {
                node().anyBlock();
            }
        });
        which.add(choice);
        which.add(any);
        panel.add(which);

        JPanel where = row();
        survey.setForeground(INK);
        JButton set = button("Set survey position", PRIMARY, Color.WHITE);
        set.setToolTipText("optional: where the robot looks from first (camera ≥ 0.25 m above the blocks); "
                + "without one it looks from wherever the arm is");
        JButton go = button("Move there", null, null);
        JButton clear = button("Clear", null, null);
        clear.setToolTipText("forget the survey position: look from wherever the arm is");
        on(clear, new Runnable() {
            @Override
            public void run() {
                node().clearSurvey();
            }
        });
        on(set, new Runnable() {
            @Override
            public void run() {
                node().setSurvey();
            }
        });
        on(go, new Runnable() {
            @Override
            public void run() {
                node().moveToSurvey();
            }
        });
        where.add(set);
        where.add(go);
        where.add(clear);
        where.add(survey);
        panel.add(where);

        JPanel params = row();
        on(grip, new Runnable() {
            @Override
            public void run() {
                keypad(grip, node().gripMm(), true);
            }
        });
        on(lift, new Runnable() {
            @Override
            public void run() {
                keypad(lift, node().liftMm(), false);
            }
        });
        params.add(grip);
        params.add(lift);
        panel.add(params);

        JPanel checks = row();
        JButton hover = button("Check: move above it", null, null);
        JButton into = button("Check: move to the grip", null, null);
        hover.setToolTipText("PolyScope's hold-to-move screen, to where the fingertips hover 40 mm over the block");
        into.setToolTipText("…and to the grip depth — jog from there to see it lines up");
        on(hover, new Runnable() {
            @Override
            public void run() {
                node().check("hover");
            }
        });
        on(into, new Runnable() {
            @Override
            public void run() {
                node().check("grip");
            }
        });
        checks.add(hover);
        checks.add(into);
        panel.add(checks);

        status.setEditable(false);
        status.setLineWrap(true);
        status.setWrapStyleWord(true);
        status.setForeground(INK);
        status.setBackground(STATUS_BG);
        status.setBorder(BorderFactory.createEmptyBorder(6, 10, 6, 10));
        status.setAlignmentX(Component.LEFT_ALIGNMENT);
        status.setMaximumSize(new Dimension(Integer.MAX_VALUE, 110));
        panel.add(status);

        JLabel hint = new JLabel("<html>The program finds the block from where the arm is (or from the survey position, "
                + "if you set one), looks again from halfway there with the block in the middle of the "
                + "picture, and lowers the fingertips straight down into it; your gripper nodes <b>inside this node</b> "
                + "run at the grip, then it lifts. <b>rs_pick_found</b> is True after a pick — test it with an If. "
                + "The cockpit address is set once in Installation → URCaps → RealSense Pilot.</html>");
        hint.setForeground(MUTED);
        hint.setFont(hint.getFont().deriveFont(11f));
        hint.setAlignmentX(Component.LEFT_ALIGNMENT);
        hint.setMaximumSize(new Dimension(680, 80));
        panel.add(hint);
    }

    private PickContribution node() {
        return provider.get();
    }

    private void keypad(final JButton target, double initial, final boolean isGrip) {
        KeyboardNumberInput<Double> kb = api.getUserInterfaceAPI().getUserInteraction().getKeyboardInputFactory()
                .createPositiveDoubleKeypadInput();
        kb.setInitialValue(initial);
        kb.show(target, new KeyboardInputCallback<Double>() {
            @Override
            public void onOk(Double value) {
                if (value == null) return;
                if (isGrip) node().setGripMm(Math.max(0.0, Math.min(60.0, value)));
                else node().setLiftMm(Math.max(5.0, Math.min(300.0, value)));
            }
        });
    }

    // -- what the contribution shows ----------------------------------------------------------------

    /** Refresh every field from the node's data model. */
    void show(final PickContribution n) {
        onEdt(new Runnable() {
            @Override
            public void run() {
                cockpit.setText("cockpit " + n.cockpit().base + " · pick server port "
                        + n.script().port + "   (Installation → URCaps → RealSense Pilot)");
                double[] q = n.surveyJoints();
                survey.setText(q == null ? "none: first look from where the arm is" : "taught");
                choice.setText(n.tapU() >= 0 ? "Picks the block nearest the spot you tapped (" + n.tapU() + ", "
                        + n.tapV() + ")" : "Picks the block nearest the middle — or tap one in the picture");
                grip.setText(String.format(Locale.ROOT, "Grip depth: %.0f mm below the top", n.gripMm()));
                lift.setText(String.format(Locale.ROOT, "Lift: %.0f mm", n.liftMm()));
                feed.tapU = n.tapU();
                feed.tapV = n.tapV();
                feed.repaint();
            }
        });
    }

    void setStatus(final String text, final PilotView.Kind kind) {
        onEdt(new Runnable() {
            @Override
            public void run() {
                status.setText(text);
                status.setBackground(kind == PilotView.Kind.OK ? OK_BG : kind == PilotView.Kind.WARN ? WARN_BG
                        : kind == PilotView.Kind.ERR ? ERR_BG : STATUS_BG);
            }
        });
    }

    void setLive(final boolean live, final String framesPerSecond) {
        onEdt(new Runnable() {
            @Override
            public void run() {
                dot.setForeground(live ? LIVE : DEAD);
                String f = "";
                if (live && framesPerSecond != null) {
                    try {
                        f = String.format(Locale.ROOT, "%.1f fps", Double.parseDouble(framesPerSecond));
                    } catch (NumberFormatException e) {
                        f = "";
                    }
                }
                fps.setText(f);
            }
        });
    }

    void setFrame(final BufferedImage image) {
        onEdt(new Runnable() {
            @Override
            public void run() {
                feed.image = image;
                feed.repaint();
            }
        });
    }

    void setBlocks(final List<int[]> blocks, final int tapU, final int tapV) {
        onEdt(new Runnable() {
            @Override
            public void run() {
                feed.blocks = new ArrayList<int[]>(blocks);
                feed.tapU = tapU;
                feed.tapV = tapV;
                feed.repaint();
            }
        });
    }

    private static void onEdt(Runnable r) {
        if (SwingUtilities.isEventDispatchThread()) r.run();
        else SwingUtilities.invokeLater(r);
    }

    private static void on(JButton b, final Runnable action) {
        b.addActionListener(new ActionListener() {
            @Override
            public void actionPerformed(ActionEvent e) {
                action.run();
            }
        });
    }

    private static JPanel row() {
        JPanel p = new JPanel(new FlowLayout(FlowLayout.LEFT, 8, 4));
        p.setOpaque(false);
        p.setAlignmentX(Component.LEFT_ALIGNMENT);
        return p;
    }

    private static JButton button(String text, Color bg, Color fg) {
        JButton b = new JButton(text);
        b.setFont(b.getFont().deriveFont(15f));
        b.setMargin(new Insets(8, 14, 8, 14));
        if (bg != null) {
            b.setBackground(bg);
            b.setForeground(fg);
            b.setOpaque(true);
        }
        return b;
    }

    /** The colour feed scaled to fit, the detected blocks, and the chosen spot. */
    private final class Feed extends JComponent {
        private static final long serialVersionUID = 1L;
        private static final int W = 640;
        private static final int H = 362; // 848×480 scaled to 640 wide
        transient volatile BufferedImage image;
        transient volatile List<int[]> blocks = new ArrayList<int[]>();
        int tapU = -1;
        int tapV = -1;

        Feed() {
            setPreferredSize(new Dimension(W, H));
            setMaximumSize(new Dimension(W, H));
            setMinimumSize(new Dimension(W / 2, H / 2));
            addMouseListener(new MouseAdapter() {
                @Override
                public void mouseClicked(MouseEvent e) {
                    int[] px = pixelOf(e);
                    if (px != null && provider != null) node().onTap(px[0], px[1]);
                }
            });
        }

        private double scale() {
            BufferedImage img = image;
            if (img == null) return 0;
            return Math.min(getWidth() / (double) img.getWidth(), getHeight() / (double) img.getHeight());
        }

        private int[] pixelOf(MouseEvent e) {
            BufferedImage img = image;
            double s = scale();
            if (img == null || s <= 0) return null;
            int x = (int) Math.round(e.getX() / s);
            int y = (int) Math.round(e.getY() / s);
            if (x < 0 || y < 0 || x >= img.getWidth() || y >= img.getHeight()) return null;
            return new int[] {x, y};
        }

        @Override
        protected void paintComponent(Graphics g0) {
            Graphics2D g = (Graphics2D) g0.create();
            g.setRenderingHint(RenderingHints.KEY_INTERPOLATION, RenderingHints.VALUE_INTERPOLATION_BILINEAR);
            g.setRenderingHint(RenderingHints.KEY_ANTIALIASING, RenderingHints.VALUE_ANTIALIAS_ON);
            g.setColor(STAGE);
            g.fillRect(0, 0, getWidth(), getHeight());
            BufferedImage img = image;
            double s = scale();
            if (img != null && s > 0) {
                Rectangle r = new Rectangle(0, 0, (int) Math.round(img.getWidth() * s), (int) Math.round(img.getHeight() * s));
                g.drawImage(img, r.x, r.y, r.width, r.height, null);
                int[] chosen = chosen();
                int n = 0;
                g.setFont(getFont().deriveFont(Font.BOLD, 13f));
                for (int[] b : blocks) {
                    n++;
                    int x = (int) Math.round(b[0] * s);
                    int y = (int) Math.round(b[1] * s);
                    boolean pick = b == chosen;
                    g.setStroke(new BasicStroke(pick ? 3f : 2f));
                    g.setColor(pick ? MARK : BLOCK);
                    g.drawOval(x - 16, y - 16, 32, 32);
                    g.drawString(String.valueOf(n), x + 18, y - 10);
                }
                if (tapU >= 0) {
                    int x = (int) Math.round(tapU * s);
                    int y = (int) Math.round(tapV * s);
                    g.setColor(MARK);
                    g.setStroke(new BasicStroke(2f));
                    g.drawLine(x - 8, y, x + 8, y);
                    g.drawLine(x, y - 8, x, y + 8);
                }
            }
            String hint = blocks.isEmpty() ? "no block detected — the detector finds white blocks"
                    : blocks.size() + " block" + (blocks.size() == 1 ? "" : "s") + " seen · tap one to choose it";
            g.setFont(getFont().deriveFont(12f));
            int tw = g.getFontMetrics().stringWidth(hint);
            g.setColor(new Color(15, 22, 32, 204));
            g.fillRoundRect(8, getHeight() - 30, tw + 16, 22, 8, 8);
            g.setColor(new Color(0xe8eef6));
            g.drawString(hint, 16, getHeight() - 14);
            g.dispose();
        }

        /** The block the program would take now: nearest the tap, else nearest the middle. */
        private int[] chosen() {
            BufferedImage img = image;
            if (img == null || blocks.isEmpty()) return null;
            double u = tapU >= 0 ? tapU : img.getWidth() / 2.0;
            double v = tapU >= 0 ? tapV : img.getHeight() / 2.0;
            int[] best = null;
            double bestD = Double.MAX_VALUE;
            for (int[] b : blocks) {
                double d = (b[0] - u) * (b[0] - u) + (b[1] - v) * (b[1] - v);
                if (d < bestD) {
                    bestD = d;
                    best = b;
                }
            }
            return best;
        }
    }
}
