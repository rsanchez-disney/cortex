import { Routes } from '@angular/router';

export const routes: Routes = [
  { path: '', redirectTo: 'dashboard', pathMatch: 'full' },
  { path: 'dashboard', component: DashboardComponent },
  { path: 'payments', loadChildren: () => import('./payments/payments.module').then(m => m.PaymentsModule) },
  { path: 'config', component: ConfigComponent },
  { path: 'reports', loadChildren: () => import('./reports/reports.routes').then(m => m.REPORT_ROUTES) },
];
