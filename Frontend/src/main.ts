import { bootstrapApplication } from "@angular/platform-browser";
import { AppComponent } from "./app/app.component";
import { appConfig } from "./app/app.config";
import { LoggerService } from "./app/services/logger.service";

// Initialize logger early to capture bootstrap errors
let logger: LoggerService | null = null;

try {
  // LoggerService will be provided by DI, but we can't use it here
  // Instead, use console for bootstrap errors only
  bootstrapApplication(AppComponent, appConfig).catch((err) => {
    console.error("Bootstrap error:", err);
  });
} catch (err) {
  console.error("Failed to bootstrap application:", err);
}
