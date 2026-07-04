import { Injectable, Logger } from '@nestjs/common';
import { CaptchaGateway } from './captcha.gateway';
import { CaptchaAction } from 'src/constant';

@Injectable()
export class CaptchaService {
  constructor(private readonly captchaGateway: CaptchaGateway) {}
  private logger = new Logger(CaptchaService.name);
  async getCaptcha(
    action: CaptchaAction = CaptchaAction.VIDEO_GENERATION,
    isDebug: boolean = false,
  ): Promise<string> {
    try {
      const token = await this.captchaGateway.requestCaptcha(action);
      return token;
    } catch (error) {
      this.logger.error('Failed to get captcha:', error);
      throw error;
    }
  }

  async forceRefresh() {
    this.logger.log('Forcing captcha refresh...');
    await this.captchaGateway.forceRefresh();
  }
}
