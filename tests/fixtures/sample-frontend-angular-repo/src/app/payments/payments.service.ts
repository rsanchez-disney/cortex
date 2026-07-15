import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import { Payment, PaymentRequest } from '../models/payment.model';

@Injectable({
  providedIn: 'root'
})
export class PaymentsService {
  private readonly baseUrl = `${environment.apiUrl}/api/v1/payments`;

  constructor(private http: HttpClient) {}

  getAll(): Observable<Payment[]> {
    return this.http.get<Payment[]>(`${environment.apiUrl}/api/v1/payments`);
  }

  getById(id: string): Observable<Payment> {
    return this.http.get<Payment>(`${environment.apiUrl}/api/v1/payments/${id}`);
  }

  create(request: PaymentRequest): Observable<Payment> {
    return this.http.post<Payment>(`${environment.apiUrl}/api/v1/payments`, request);
  }

  update(id: string, request: PaymentRequest): Observable<Payment> {
    return this.http.put<Payment>(`${environment.apiUrl}/api/v1/payments/${id}`, request);
  }

  delete(id: string): Observable<void> {
    return this.http.delete<void>(`${environment.apiUrl}/api/v1/payments/${id}`);
  }
}
