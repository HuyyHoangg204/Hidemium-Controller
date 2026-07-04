import { Module } from '@nestjs/common';
import { CaptchaService } from './captcha.service';
import { CaptchaController } from './captcha.controller';
import { CaptchaGateway } from './captcha.gateway';

@Module({
  controllers: [CaptchaController],
  providers: [CaptchaService, CaptchaGateway],
  exports: [CaptchaService],
})
export class CaptchaModule {}
