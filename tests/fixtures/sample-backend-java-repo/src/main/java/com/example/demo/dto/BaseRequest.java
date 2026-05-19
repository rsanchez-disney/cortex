package com.example.demo.dto;

import jakarta.validation.constraints.NotNull;

public abstract class BaseRequest {
    @NotNull
    private String requestId;

    private String correlationId;

    private long timestamp;
}
