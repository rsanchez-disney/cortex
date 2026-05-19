package com.example.demo.dto;

import javax.validation.constraints.NotNull;
import javax.validation.constraints.Min;

public class OrderItemDto {

    @NotNull
    private String productId;

    @NotNull
    private String productName;

    @Min(1)
    private int quantity;

    private java.math.BigDecimal unitPrice;
}
