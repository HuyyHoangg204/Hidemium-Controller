import {
  WebSocketGateway,
  WebSocketServer,
  SubscribeMessage,
  OnGatewayConnection,
  OnGatewayDisconnect,
} from '@nestjs/websockets';
import { Server, Socket } from 'socket.io';
import { Logger } from '@nestjs/common';
import { CaptchaAction } from 'src/constant';

@WebSocketGateway({
  cors: {
    origin: '*',
  },
})
export class CaptchaGateway
  implements OnGatewayConnection, OnGatewayDisconnect
{
  @WebSocketServer()
  server: Server;

  private logger = new Logger(CaptchaGateway.name);
  private connectedClients = new Map<string, Socket>();
  private pendingRequests = new Map<string, any>();

  handleConnection(client: Socket) {
    this.logger.log(`✅ Client connected: ${client.id}`);
    this.connectedClients.set(client.id, client);
  }

  handleDisconnect(client: Socket) {
    this.logger.log(`❌ Client disconnected: ${client.id}`);
    this.connectedClients.delete(client.id);
  }

  @SubscribeMessage('client:ready')
  handleClientReady(client: Socket, data: any) {
    this.logger.log(`🎉 Client ready: ${client.id}`);
  }

  @SubscribeMessage('client:captcha-solved')
  handleCaptchaSolved(client: Socket, data: any) {
    this.logger.log(`✅ Captcha solved: ${data.token.substring(0, 50)}...`);

    // Resolve pending request nếu có
    if (data.requestId && this.pendingRequests.has(data.requestId)) {
      const { resolve } = this.pendingRequests.get(data.requestId);
      resolve(data.token);
      this.pendingRequests.delete(data.requestId);
    }
  }

  @SubscribeMessage('client:captcha-error')
  handleCaptchaError(client: Socket, data: any) {
    this.logger.error(`❌ Captcha error: ${data.error}`);

    if (data.requestId && this.pendingRequests.has(data.requestId)) {
      const { reject } = this.pendingRequests.get(data.requestId);
      reject(new Error(data.error));
      this.pendingRequests.delete(data.requestId);
    }
  }

  @SubscribeMessage('client:captcha-manual')
  handleCaptchaManual(client: Socket, data: any) {
    this.logger.log(`📨 Manual captcha: ${data.token.substring(0, 50)}...`);
  }

  // Method để request captcha từ service khác
  async requestCaptcha(
    action: CaptchaAction,
    timeout = 30000,
  ): Promise<string> {
    const clients = Array.from(this.connectedClients.values());

    if (clients.length === 0) {
      throw new Error('No browser clients connected');
    }

    const client = clients[0]; // Lấy client đầu tiên
    const requestId = `req_${Date.now()}`;

    return new Promise((resolve, reject) => {
      // Store pending request
      this.pendingRequests.set(requestId, { resolve, reject });

      // Set timeout
      const timer = setTimeout(() => {
        if (this.pendingRequests.has(requestId)) {
          this.pendingRequests.delete(requestId);
          reject(new Error('Captcha request timeout'));
        }
      }, timeout);

      // Send request to client
      console.log('Sending captcha request with action:', action);
      client.emit('server:request-captcha', { requestId, action });

      this.logger.log(`📤 Sent captcha request: ${requestId}`);
    });
  }

  async forceRefresh() {
    this.logger.log('🔄 Forcing captcha refresh on all clients...');
    this.connectedClients.forEach((client) => {
      client.emit('server:reload-page');
    });
  }
}
