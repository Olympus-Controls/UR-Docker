package com.olympuscontrols.realsensepilot;

import com.ur.urcap.api.contribution.installation.swing.SwingInstallationNodeService;
import com.ur.urcap.api.contribution.program.swing.SwingProgramNodeService;
import org.osgi.framework.BundleActivator;
import org.osgi.framework.BundleContext;

/**
 * Registers the RealSense Pilot installation node, the RealSense Pick program node and its
 * "After picture N" child with PolyScope 5.
 */
public class Activator implements BundleActivator {
    @Override
    public void start(BundleContext context) {
        context.registerService(SwingInstallationNodeService.class, new PilotService(), null);
        context.registerService(SwingProgramNodeService.class, new PickService(), null);
        context.registerService(SwingProgramNodeService.class, new PickRoutineService(), null);
    }

    @Override
    public void stop(BundleContext context) {
        // nothing held outside the nodes, which PolyScope closes itself
    }
}
