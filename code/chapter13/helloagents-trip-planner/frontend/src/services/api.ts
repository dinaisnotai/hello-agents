import axios from 'axios'
import type {
  ChatMessageResponse,
  ReplanRequest,
  TripFormData,
  TripPlanResponse,
  TripSessionResponse,
} from '@/types'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'
const configuredTimeout = Number(import.meta.env.VITE_API_TIMEOUT_MS || 300000)
const API_TIMEOUT_MS = Number.isFinite(configuredTimeout) && configuredTimeout > 0
  ? configuredTimeout
  : 300000

const apiClient = axios.create({
  baseURL: API_BASE_URL,
  timeout: API_TIMEOUT_MS,
  headers: {
    'Content-Type': 'application/json'
  }
})

// 请求拦截器
apiClient.interceptors.request.use(
  (config) => {
    console.log('发送请求:', config.method?.toUpperCase(), config.url)
    return config
  },
  (error) => {
    console.error('请求错误:', error)
    return Promise.reject(error)
  }
)

// 响应拦截器
apiClient.interceptors.response.use(
  (response) => {
    console.log('收到响应:', response.status, response.config.url)
    return response
  },
  (error) => {
    console.error('响应错误:', error.response?.status, error.message, {
      code: error.code,
      timeoutMs: error.config?.timeout
    })
    return Promise.reject(error)
  }
)

/**
 * 生成旅行计划
 */
export async function generateTripPlan(formData: TripFormData): Promise<TripPlanResponse> {
  try {
    const response = await apiClient.post<TripPlanResponse>('/api/trip/plan', formData)
    return response.data
  } catch (error: any) {
    console.error('生成旅行计划失败:', error)
    if (error.code === 'ECONNABORTED') {
      throw new Error(`生成旅行计划超过 ${Math.round(API_TIMEOUT_MS / 1000)} 秒，请查看后端各 Agent 耗时日志`)
    }
    throw new Error(error.response?.data?.detail || error.message || '生成旅行计划失败')
  }
}

/**
 * Recalculate route, budget, and constraints after user edits.
 */
export async function replanTrip(payload: ReplanRequest): Promise<TripPlanResponse> {
  try {
    const response = await apiClient.post<TripPlanResponse>('/api/trip/replan', payload)
    return response.data
  } catch (error: any) {
    console.error('重规划失败:', error)
    throw new Error(error.response?.data?.detail || error.message || '重规划失败')
  }
}

export async function createTripSession(formData: TripFormData): Promise<TripSessionResponse> {
  try {
    const response = await apiClient.post<TripSessionResponse>('/api/trip/sessions', formData)
    return response.data
  } catch (error: any) {
    throw new Error(error.response?.data?.detail || error.message || '创建旅行会话失败')
  }
}

export async function getTripSession(sessionId: string): Promise<TripSessionResponse> {
  try {
    const response = await apiClient.get<TripSessionResponse>(`/api/trip/sessions/${sessionId}`)
    return response.data
  } catch (error: any) {
    throw new Error(error.response?.data?.detail || error.message || '加载旅行会话失败')
  }
}

export async function sendTripMessage(
  sessionId: string,
  content: string,
): Promise<ChatMessageResponse> {
  try {
    const response = await apiClient.post<ChatMessageResponse>(
      `/api/trip/sessions/${sessionId}/messages`,
      { content },
    )
    return response.data
  } catch (error: any) {
    throw new Error(error.response?.data?.detail || error.message || '修改行程失败')
  }
}

export async function replanTripSession(
  sessionId: string,
  payload: ReplanRequest,
): Promise<TripSessionResponse> {
  try {
    const response = await apiClient.post<TripSessionResponse>(
      `/api/trip/sessions/${sessionId}/replan`,
      payload,
    )
    return response.data
  } catch (error: any) {
    throw new Error(error.response?.data?.detail || error.message || '重规划失败')
  }
}

/**
 * 健康检查
 */
export async function healthCheck(): Promise<any> {
  try {
    const response = await apiClient.get('/health')
    return response.data
  } catch (error: any) {
    console.error('健康检查失败:', error)
    throw new Error(error.message || '健康检查失败')
  }
}

export default apiClient

