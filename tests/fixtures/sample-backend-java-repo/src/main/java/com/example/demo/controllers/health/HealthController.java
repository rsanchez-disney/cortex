package com.example.demo.controllers.health;

import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/v1")
@Tag(name = "Health")
public class HealthController {

    @GetMapping("/health")
    @Operation(summary = "Health check endpoint")
    public ResponseEntity<?> health() {
        return ResponseEntity.ok().build();
    }

    @GetMapping("/ready")
    @Operation(summary = "Readiness probe")
    public ResponseEntity<?> ready() {
        return ResponseEntity.ok().build();
    }
}
