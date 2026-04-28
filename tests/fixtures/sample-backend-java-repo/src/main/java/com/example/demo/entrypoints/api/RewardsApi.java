package com.example.demo.entrypoints.api;

import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import org.springframework.web.bind.annotation.*;

/**
 * API interface for the rewards domain.
 * Route annotations live here; the controller only has @RestController + @Override.
 */
@Tag(name = "Rewards")
@RequestMapping(path = "/v1/rewards")
public interface RewardsApi {

    @Operation(description = "List all available rewards")
    @GetMapping
    Object listRewards();

    @Operation(description = "Redeem a reward by ID")
    @PostMapping("/{rewardId}/redeem")
    Object redeemReward(@PathVariable String rewardId);

    @Operation(summary = "Get reward details")
    @GetMapping("/{rewardId}")
    Object getReward(@PathVariable String rewardId);
}
