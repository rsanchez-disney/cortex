package com.example.demo.dataproviders.webclients;

import org.springframework.web.service.annotation.GetExchange;
import org.springframework.web.service.annotation.HttpExchange;
import org.springframework.web.service.annotation.PostExchange;

@HttpExchange
public interface TicketingWebClient {

    @GetExchange(url = "/v1/tickets")
    Object listTickets();

    @PostExchange(url = "/v1/tickets/purchase")
    Object purchaseTicket(Object body);

    @GetExchange(url = "/v1/tickets/{ticketId}")
    Object getTicket(String ticketId);
}
