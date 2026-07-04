import { ApiProperty } from '@nestjs/swagger';
import { IsBoolean, IsOptional } from 'class-validator';
import { CaptchaAction } from 'src/constant';

export class GetCaptchaQuery {
  @ApiProperty({
    required: false,
  })
  @IsOptional()
  @IsBoolean()
  isDebug?: boolean;

  @ApiProperty({
    required: false,
    enum: CaptchaAction,
    description: 'The action type for the captcha request',
  })
  @IsOptional()
  action?: CaptchaAction;
}
