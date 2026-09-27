package com.olympuscontrols.realsensepilot;

import com.ur.urcap.api.contribution.installation.swing.SwingInstallationNodeService;
import org.osgi.framework.BundleActivator;
import org.osgi.framework.BundleContext;

/** Registers the RealSense Pilot installation node with PolyScope 5. */
public class Activator implements BundleActivator {
    @Override
    public void start(BundleContext context) {
        context.registerService(SwingInstallationNodeService.class, new PilotService(), null);
    }

    @Override
    public void stop(BundleContext context) {
        // nothing held outside the node, which PolyScope closes itself
    }
}
