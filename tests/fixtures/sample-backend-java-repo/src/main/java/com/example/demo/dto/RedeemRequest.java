package com.example.demo.dto;

import javax.validation.constraints.NotNull;

public class RedeemRequest {

    @NotNull
    private String rewardId;

    @NotNull
    private String memberId;

    private int quantity;
}
