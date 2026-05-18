package com.example.demo.entrypoints.api;

import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import java.util.List;

/**
 * API interface for the rewards domain.
 * Route annotations live here; the controller only has @RestController + @Override.
 */
@Tag(name = "Rewards")
@RequestMapping(path = "/v1/rewards")
public interface RewardsApi {

    @Operation(description = "List all available rewards")
    @GetMapping
    ResponseEntity<List<RewardDto>> listRewards(
            @RequestParam("memberId") String memberId,
            @RequestParam(value = "limit", required = false, defaultValue = "10") int limit);

    @Operation(description = "Redeem a reward by ID")
    @PostMapping("/{rewardId}/redeem")
    ResponseEntity<RedemptionResult> redeemReward(
            @PathVariable String rewardId,
            @RequestBody RedeemRequest request);

    @Operation(summary = "Get reward details")
    @GetMapping("/{rewardId}")
    ResponseEntity<RewardDto> getReward(@PathVariable String rewardId);
}
