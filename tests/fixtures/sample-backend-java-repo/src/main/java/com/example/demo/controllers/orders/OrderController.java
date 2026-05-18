package com.example.demo.controllers.orders;

import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import java.util.List;

@RestController
@RequestMapping("/v1/orders")
@Tag(name = "Orders")
public class OrderController {

    @GetMapping
    @Operation(summary = "List all orders")
    public ResponseEntity<List<OrderDto>> listOrders(
            @RequestParam(value = "status", required = false) String status,
            @RequestParam(defaultValue = "0") int page) {
        return ResponseEntity.ok().build();
    }

    @PostMapping
    @Operation(summary = "Create a new order")
    public ResponseEntity<OrderDto> createOrder(@RequestBody CreateOrderRequest request) {
        return ResponseEntity.ok().build();
    }

    @GetMapping("/{id}")
    @Operation(summary = "Get order by ID")
    public ResponseEntity<OrderDto> getOrder(@PathVariable String id) {
        return ResponseEntity.ok().build();
    }

    @DeleteMapping("/{id}")
    @Operation(summary = "Cancel an order")
    public ResponseEntity<Void> cancelOrder(
            @PathVariable String id,
            @RequestHeader("X-Correlation-Id") String correlationId) {
        return ResponseEntity.ok().build();
    }
}
