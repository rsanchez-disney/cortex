package com.example.demo.listeners;

import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

@Component
public class OrderEventListener {

    @KafkaListener(topics = "${kafka.topics.order-created}", groupId = "demo-group")
    public void onOrderCreated(String message) {
        // Process order created event
    }

    @Scheduled(cron = "0 0 * * * *")
    public void cleanupExpiredOrders() {
        // Hourly cleanup job
    }
}
