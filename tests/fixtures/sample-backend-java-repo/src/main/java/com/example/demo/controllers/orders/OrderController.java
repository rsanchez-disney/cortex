package com.example.demo.controllers.orders;

import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/v1/orders")
@Tag(name = "Orders")
public class OrderController {

    @GetMapping
    @Operation(summary = "List all orders")
    public ResponseEntity<?> listOrders() {
        return ResponseEntity.ok().build();
    }

    @PostMapping
    @Operation(summary = "Create a new order")
    public ResponseEntity<?> createOrder() {
        return ResponseEntity.ok().build();
    }

    @GetMapping("/{id}")
    @Operation(summary = "Get order by ID")
    public ResponseEntity<?> getOrder(@PathVariable String id) {
        return ResponseEntity.ok().build();
    }

    @DeleteMapping("/{id}")
    @Operation(summary = "Cancel an order")
    public ResponseEntity<?> cancelOrder(@PathVariable String id) {
        return ResponseEntity.ok().build();
    }
}
