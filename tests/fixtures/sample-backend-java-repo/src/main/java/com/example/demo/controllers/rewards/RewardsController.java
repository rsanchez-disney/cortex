package com.example.demo.controllers.rewards;

import com.example.demo.entrypoints.api.RewardsApi;
import org.springframework.web.bind.annotation.RestController;

/**
 * Controller that delegates route mapping to RewardsApi interface.
 * No @*Mapping annotations here — they live on the interface.
 */
@RestController
public class RewardsController implements RewardsApi {

    @Override
    public Object listRewards() {
        return java.util.Collections.emptyList();
    }

    @Override
    public Object redeemReward(String rewardId) {
        return "redeemed";
    }

    @Override
    public Object getReward(String rewardId) {
        return null;
    }
}
