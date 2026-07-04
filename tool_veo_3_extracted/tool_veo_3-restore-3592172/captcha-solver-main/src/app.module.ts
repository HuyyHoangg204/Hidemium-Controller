import { Module } from '@nestjs/common';
import { CaptchaModule } from './modules/captcha/captcha.module';
import { AppController } from './app.controller';

@Module({
  imports: [CaptchaModule],
  controllers: [AppController],
})
export class AppModule {}
