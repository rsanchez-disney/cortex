package com.example.demo.controllers.rewards;

import com.example.demo.entrypoints.api.RewardsApi;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.RestController;
import java.util.List;

/**
 * Controller that delegates route mapping to RewardsApi interface.
 * No @*Mapping annotations here — they live on the interface.
 */
@RestController
public class RewardsController implements RewardsApi {

    @Override
    public ResponseEntity<List<RewardDto>> listRewards(String memberId, int limit) {
        return ResponseEntity.ok(java.util.Collections.emptyList());
    }

    @Override
    public ResponseEntity<RedemptionResult> redeemReward(String rewardId, RedeemRequest request) {
        return ResponseEntity.ok().build();
    }

    @Override
    public ResponseEntity<RewardDto> getReward(String rewardId) {
        return ResponseEntity.ok().build();
    }
}
