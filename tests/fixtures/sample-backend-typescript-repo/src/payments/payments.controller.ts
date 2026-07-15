import { Controller, Get, Post, Body, Param } from '@nestjs/common';
import { CreatePaymentDto } from './dto/create-payment.dto';

export interface Payment {
  id: string;
  amount: number;
  currency: string;
  status: string;
}

@Controller('payments')
export class PaymentsController {
  @Post()
  async create(@Body() createPaymentDto: CreatePaymentDto): Promise<Payment> {
    // Create a new payment
    return {
      id: '123',
      amount: createPaymentDto.amount,
      currency: createPaymentDto.currency,
      status: 'pending',
    };
  }

  @Get()
  async findAll(): Promise<Payment[]> {
    // Return all payments
    return [];
  }

  @Get(':id')
  async findOne(@Param('id') id: string): Promise<Payment> {
    // Return a single payment
    return { id, amount: 100, currency: 'USD', status: 'completed' };
  }
}
