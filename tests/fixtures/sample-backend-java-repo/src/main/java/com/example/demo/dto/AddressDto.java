package com.example.demo.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

public class AddressDto {
    @NotBlank
    private String street;

    @NotBlank
    private String city;

    @Size(min = 2, max = 2)
    private String state;

    @NotBlank
    private String zipCode;

    private String country;
}
