import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { wsService } from '@/services/websocket'

// Global State
const username = ref<string | null>(localStorage.getItem('auth_username'))
// 内网部署免登录：标志位缺省视为已登录（后端 API 本就无鉴权中间件，此门面仅 UX 用）；
// 显式 logout 后写入 'false'，下次才回到登录页。
const isLoggedIn = ref(localStorage.getItem('auth_logged_in') !== 'false')

// 免登录直进时 WS 不会经 setAuthenticated 启动，模块加载即补启
if (isLoggedIn.value) {
  wsService.start()
}

export function useAuth() {
  const router = useRouter()

  const isAuthenticated = computed(() => isLoggedIn.value)

  function setAuthenticated(user: string) {
    username.value = user
    isLoggedIn.value = true

    localStorage.setItem('auth_username', user)
    localStorage.setItem('auth_logged_in', 'true')

    // 启动 WebSocket 连接
    wsService.start()
  }

  function logout() {
    username.value = null
    isLoggedIn.value = false
    localStorage.removeItem('auth_username')
    localStorage.removeItem('auth_logged_in')

    // 停止 WebSocket 连接
    wsService.stop()

    // Redirect to login if using router
    if (router) {
      router.push('/login')
    } else {
      window.location.href = '/login'
    }
  }

  async function login(user: string, pass: string): Promise<boolean> {
    try {
      const basePath = (import.meta.env.BASE_URL ?? '/').replace(/\/+$/, '')
      const response = await fetch(`${basePath}/auth/status`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ username: user, password: pass }),
      })

      if (response.ok) {
        setAuthenticated(user)
        return true
      } else {
        return false
      }
    } catch (e) {
      console.error('Login error', e)
      return false
    }
  }

  return {
    username,
    isAuthenticated,
    login,
    logout
  }
}
