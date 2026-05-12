package com.example.demo.publishers;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.stereotype.Component;

import com.example.demo.config.OrderTopics;

@Component
public class OrderEventPublisher {
    private final KafkaTemplate<String, String> kafkaTemplate;

    @Autowired
    public OrderEventPublisher(KafkaTemplate<String, String> kafkaTemplate) {
        this.kafkaTemplate = kafkaTemplate;
    }

    public void publishOrderCreated(String orderId) {
        kafkaTemplate.send("${kafka.topics.order-created}", orderId);
    }

    public void publishOrderShipped(String orderId) {
        kafkaTemplate.send(OrderTopics.ORDER_SHIPPED, orderId);
    }
}
