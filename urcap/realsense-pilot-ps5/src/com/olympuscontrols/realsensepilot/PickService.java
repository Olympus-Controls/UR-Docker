package com.olympuscontrols.realsensepilot;

import com.ur.urcap.api.contribution.ViewAPIProvider;
import com.ur.urcap.api.contribution.program.ContributionConfiguration;
import com.ur.urcap.api.contribution.program.CreationContext;
import com.ur.urcap.api.contribution.program.ProgramAPIProvider;
import com.ur.urcap.api.contribution.program.swing.SwingProgramNodeService;
import com.ur.urcap.api.domain.data.DataModel;
import java.util.Locale;

/**
 * Program tab → URCaps → RealSense Pick: survey, choose the block, pick it. The
 * operator's gripper nodes go inside it as children and run at the grip.
 */
public class PickService implements SwingProgramNodeService<PickContribution, PickView> {
    @Override
    public String getId() {
        return "RealSensePick";
    }

    @Override
    public void configureContribution(ContributionConfiguration configuration) {
        configuration.setChildrenAllowed(true);
        configuration.setUserInsertable(true);
    }

    @Override
    public String getTitle(Locale locale) {
        return "RealSense Pick";
    }

    @Override
    public PickView createView(ViewAPIProvider apiProvider) {
        return new PickView(apiProvider);
    }

    @Override
    public PickContribution createNode(
            ProgramAPIProvider apiProvider, PickView view, DataModel model, CreationContext context) {
        return new PickContribution(apiProvider, view, model, context);
    }
}
