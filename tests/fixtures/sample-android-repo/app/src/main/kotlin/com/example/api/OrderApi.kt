package com.example.api

import retrofit2.http.Body
import retrofit2.http.GET
import retrofit2.http.POST
import retrofit2.http.Path

interface OrderApi {
    @GET("/v1/orders")
    suspend fun getOrders(): List<Order>

    @POST("/v1/orders")
    suspend fun createOrder(@Body order: OrderRequest): Order

    @GET("/v1/orders/{id}")
    suspend fun getOrder(@Path("id") id: String): Order
}
