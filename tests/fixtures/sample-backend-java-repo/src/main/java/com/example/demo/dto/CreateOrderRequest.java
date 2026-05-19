package com.example.demo.dto;

import javax.validation.constraints.NotNull;
import javax.validation.constraints.NotBlank;
import javax.validation.constraints.Size;
import javax.validation.constraints.Min;
import com.fasterxml.jackson.annotation.JsonProperty;
import com.fasterxml.jackson.annotation.JsonIgnore;
import io.swagger.v3.oas.annotations.media.Schema;
import java.math.BigDecimal;
import java.util.List;

@Schema(description = "Request to create a new order")
public class CreateOrderRequest {

    @NotBlank
    @Size(min = 1, max = 100)
    @Schema(description = "Customer identifier")
    private String customerId;

    @NotNull
    @JsonProperty("order_items")
    private List<OrderItemDto> items;

    @Min(0)
    private BigDecimal totalAmount;

    private String notes;

    @JsonIgnore
    private String internalTrackingId;
}
