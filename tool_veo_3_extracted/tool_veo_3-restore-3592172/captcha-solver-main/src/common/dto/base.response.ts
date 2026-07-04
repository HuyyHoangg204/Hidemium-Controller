export class BaseResponse<T> {
  success: boolean;
  data?: T;
  message?: string;

  constructor(partial: Partial<BaseResponse<T>>) {
    Object.assign(this, partial);
  }
}
