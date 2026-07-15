import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import { AppConfig } from '../models/config.model';

@Injectable({
  providedIn: 'root'
})
export class ConfigService {
  constructor(private http: HttpClient) {}

  getConfig(): Observable<AppConfig> {
    return this.http.get<AppConfig>(`${environment.apiUrl}/api/v1/config`);
  }

  updateConfig(config: Partial<AppConfig>): Observable<AppConfig> {
    return this.http.put<AppConfig>(`${environment.apiUrl}/api/v1/config`, config);
  }

  getFeatureFlags(): Observable<Record<string, boolean>> {
    return this.http.get<Record<string, boolean>>(`${environment.apiUrl}/api/v1/config/features`);
  }
}
