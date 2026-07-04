import { ApiProperty } from '@nestjs/swagger';
import { IsNotEmpty, IsString } from 'class-validator';

export class UpdateEnvDto {
  @ApiProperty({
    description: 'The VEO3 Project ID for captcha solving',
    example: '26685d88-6680-4f20-b9f4-894a1340f3a5',
  })
  @IsString()
  @IsNotEmpty()
  projectId: string;

  @ApiProperty({
    description: 'The browser session in Base64 encoding',
    example: 'UEsDBBQACAAIAGJ5... (truncated for brevity)',
  })
  @IsString()
  @IsNotEmpty()
  sessionInBase64: string;
}
