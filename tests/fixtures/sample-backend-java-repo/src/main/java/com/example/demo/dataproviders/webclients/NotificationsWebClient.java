package com.example.demo.dataproviders.webclients;

import org.springframework.web.service.annotation.GetExchange;
import org.springframework.web.service.annotation.HttpExchange;
import org.springframework.web.service.annotation.PostExchange;

@HttpExchange
public interface NotificationsWebClient {

    @PostExchange(url = "/v1/notifications/send")
    Object sendNotification(Object body);

    @GetExchange(url = "/v1/notifications/status")
    Object getNotificationStatus();
}
