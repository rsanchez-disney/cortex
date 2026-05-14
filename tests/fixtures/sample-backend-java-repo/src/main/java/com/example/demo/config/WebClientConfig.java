package com.example.demo.config;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.reactive.function.client.WebClient;
import org.springframework.web.service.invoker.HttpServiceProxyFactory;
import reactor.netty.http.client.HttpClient;

@Configuration
public class WebClientConfig {

    @Bean
    public WebClient rewardsWebClient(@Value("${services.rewards.base-url}") String baseUrl) {
        return WebClient.builder().baseUrl(baseUrl).build();
    }

    @Bean
    public NotificationsWebClient notificationsWebClient(
            @Value("${notifications.base-uri}") String url) {
        return createWebClient(url, NotificationsWebClient.class);
    }

    @Bean
    public TicketingWebClient ticketingWebClient(
            @Value("${ticketing.base-uri}") String url) {
        return createWebClient(url, TicketingWebClient.class);
    }

    private <T> T createWebClient(String url, Class<T> clientType) {
        WebClient webClient = WebClient.builder()
                .baseUrl(url)
                .build();
        HttpServiceProxyFactory factory = HttpServiceProxyFactory.builder()
                .exchangeAdapter(null)
                .build();
        return factory.createClient(clientType);
    }
}
