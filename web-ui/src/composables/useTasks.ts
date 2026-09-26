import { ref, computed, onMounted } from 'vue'
import type {
  Task,
  TaskCreateResponse,
  TaskGenerateRequest,
  TaskGroup,
  TaskGroupCreate,
  TaskGroupUpdate,
  TaskUpdate,
} from '@/types/task.d.ts'
import * as taskApi from '@/api/tasks'
import * as groupApi from '@/api/groups'
import { useWebSocket } from '@/composables/useWebSocket'

export function useTasks() {
  const tasks = ref<Task[]>([])
  const groups = ref<TaskGroup[]>([])
  const isLoading = ref(false)
  const error = ref<Error | null>(null)
  const stoppingTaskIds = ref<Set<number>>(new Set())
  const { on } = useWebSocket()

  async function fetchTasks(options?: { silent?: boolean }) {
    if (!options?.silent) {
      isLoading.value = true
    }
    error.value = null
    try {
      tasks.value = await taskApi.getAllTasks()
    } catch (e) {
      if (e instanceof Error) {
        error.value = e
      }
      console.error(e)
    } finally {
      if (!options?.silent) {
        isLoading.value = false
      }
    }
  }

  async function fetchGroups(options?: { silent?: boolean }) {
    if (!options?.silent) {
      isLoading.value = true
    }
    error.value = null
    try {
      groups.value = await groupApi.getAllGroups()
    } catch (e) {
      if (e instanceof Error) {
        error.value = e
      }
      console.error(e)
    } finally {
      if (!options?.silent) {
        isLoading.value = false
      }
    }
  }

  const groupById = computed(() => {
    const map = new Map<number, TaskGroup>()
    groups.value.forEach((group) => map.set(group.id, group))
    return map
  })

  function groupName(task: Task | null | undefined): string | null {
    if (task?.group_id == null) return null
    return groupById.value.get(task.group_id)?.name || null
  }

  // Real-time updates
  on('tasks_updated', () => {
    fetchTasks({ silent: true })
  })

  on('task_status_changed', (data: { id: number; is_running: boolean }) => {
    const task = tasks.value.find((t) => t.id === data.id)
    if (task) {
      task.is_running = data.is_running
    }
    fetchTasks({ silent: true })
  })

  async function createTask(data: TaskGenerateRequest): Promise<TaskCreateResponse> {
    isLoading.value = true
    error.value = null
    try {
      return await taskApi.createTaskWithAI(data)
    } catch (e) {
      if (e instanceof Error) {
        error.value = e
      }
      console.error(e)
      throw e
    } finally {
      isLoading.value = false
    }
  }

  async function updateTask(taskId: number, data: TaskUpdate) {
    error.value = null
    try {
      const updatedTask = await taskApi.updateTask(taskId, data)
      const index = tasks.value.findIndex((task) => task.id === updatedTask.id)
      if (index >= 0) {
        tasks.value[index] = { ...tasks.value[index], ...updatedTask }
      } else {
        tasks.value.push(updatedTask)
      }
    } catch (e) {
      if (e instanceof Error) {
        error.value = e
      }
      console.error(e)
      throw e
    }
  }

  async function removeTask(taskId: number) {
    try {
      await taskApi.deleteTask(taskId)
      // Refresh the list after deleting
      await fetchTasks()
    } catch (e) {
      console.error(e)
      // Optionally, set the error ref to display it in the UI
      if (e instanceof Error) {
        error.value = e
      }
      throw e
    }
  }

  async function startTask(taskId: number) {
    isLoading.value = true
    const task = tasks.value.find((t) => t.id === taskId)
    const previous = task?.is_running
    if (task) {
      task.is_running = true // 乐观更新：点击后立刻显示运行中
    }
    try {
      await taskApi.startTask(taskId)
      // The websocket will update the status, but we can also optimistically update
    } catch (e) {
      if (task && previous !== undefined) {
        task.is_running = previous
      }
      if (e instanceof Error) error.value = e
      throw e
    } finally {
      isLoading.value = false
    }
  }

  async function stopTask(taskId: number) {
    isLoading.value = true
    const next = new Set(stoppingTaskIds.value)
    next.add(taskId)
    stoppingTaskIds.value = next
    try {
      await taskApi.stopTask(taskId)
    } catch (e) {
      if (e instanceof Error) error.value = e
      throw e
    } finally {
      const cleaned = new Set(stoppingTaskIds.value)
      cleaned.delete(taskId)
      stoppingTaskIds.value = cleaned
      isLoading.value = false
    }
  }

  // ---- 任务组管理 ----
  async function createGroup(data: TaskGroupCreate) {
    error.value = null
    try {
      await groupApi.createGroup(data)
      await fetchGroups({ silent: true })
    } catch (e) {
      if (e instanceof Error) error.value = e
      throw e
    }
  }

  async function updateGroup(groupId: number, data: TaskGroupUpdate) {
    error.value = null
    try {
      await groupApi.updateGroup(groupId, data)
      await fetchGroups({ silent: true })
    } catch (e) {
      if (e instanceof Error) error.value = e
      throw e
    }
  }

  async function removeGroup(groupId: number) {
    error.value = null
    try {
      await groupApi.deleteGroup(groupId)
      await fetchGroups({ silent: true })
      await fetchTasks({ silent: true })
    } catch (e) {
      if (e instanceof Error) error.value = e
      throw e
    }
  }

  async function startGroup(groupId: number) {
    error.value = null
    try {
      await groupApi.startGroup(groupId)
      await fetchTasks({ silent: true })
    } catch (e) {
      if (e instanceof Error) error.value = e
      throw e
    }
  }

  async function stopGroup(groupId: number) {
    error.value = null
    try {
      await groupApi.stopGroup(groupId)
      await fetchTasks({ silent: true })
    } catch (e) {
      if (e instanceof Error) error.value = e
      throw e
    }
  }

  // Load tasks when the composable is first used in a component
  onMounted(() => {
    fetchTasks()
    fetchGroups({ silent: true })
  })

  return {
    tasks,
    groups,
    groupById,
    groupName,
    isLoading,
    error,
    fetchTasks,
    fetchGroups,
    createTask,
    updateTask,
    removeTask,
    startTask,
    stopTask,
    stoppingTaskIds,
    createGroup,
    updateGroup,
    removeGroup,
    startGroup,
    stopGroup,
  }
}
