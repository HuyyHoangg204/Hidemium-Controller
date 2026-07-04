import { NestFactory } from '@nestjs/core';
import { AppModule } from './app.module';
import { ValidationPipe } from '@nestjs/common';
import { DocumentBuilder, SwaggerModule } from '@nestjs/swagger';

async function bootstrap() {
  const app = await NestFactory.create(AppModule);

  app.enableCors({
    origin: '*',
    credentials: true, // Cho phép gửi cookies/auth headers
    methods: ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'], // Các HTTP methods được phép
    allowedHeaders: [
      'Content-Type',
      'Authorization',
      'Accept',
      'Origin',
      'X-Requested-With',
      'X-API-Key', // Cho API Key của bạn
    ],
    exposedHeaders: ['Authorization'], // Headers mà frontend có thể đọc
    maxAge: 3600, // Cache preflight request trong 1 giờ
  });

  // Global validation pipe
  app.useGlobalPipes(
    new ValidationPipe({
      // whitelist: true,
      forbidNonWhitelisted: true,
      transform: true,
      transformOptions: {
        enableImplicitConversion: true,
      },
    }),
  );

  // Setup Swagger/OpenAPI
  const config = new DocumentBuilder()
    .setTitle('N8N Nodes Management API')
    .setDescription(
      'API untuk mengelola dan menjual n8n nodes dengan sistem tín dụ',
    )
    .setVersion('1.0.0')
    .addBearerAuth()
    .build();

  const document = SwaggerModule.createDocument(app, config);
  SwaggerModule.setup('api/docs', app, document, {
    swaggerOptions: {
      persistAuthorization: true,
      displayOperationId: true,
    },
    customCss: `.topbar { display: none; }`,
  });

  const port = process.env.API_PORT ?? 3000;
  await app.listen(port);
}
bootstrap();
