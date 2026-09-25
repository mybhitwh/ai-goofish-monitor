import type { TaskGroup, TaskGroupCreate, TaskGroupUpdate } from '@/types/task.d.ts'
import { http } from '@/lib/http'

export async function getAllGroups(): Promise<TaskGroup[]> {
  const result = await http<{ groups: TaskGroup[] }>('/api/groups')
  return result.groups
}

export async function createGroup(data: TaskGroupCreate): Promise<TaskGroup> {
  const result = await http<{ group: TaskGroup }>('/api/groups', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(data),
  })
  return result.group
}

export async function updateGroup(groupId: number, data: TaskGroupUpdate): Promise<TaskGroup> {
  const result = await http<{ group: TaskGroup }>(`/api/groups/${groupId}`, {
    method: 'PATCH',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(data),
  })
  return result.group
}

export async function deleteGroup(groupId: number): Promise<void> {
  await http(`/api/groups/${groupId}`, { method: 'DELETE' })
}

export async function startGroup(groupId: number): Promise<void> {
  await http(`/api/groups/start/${groupId}`, { method: 'POST' })
}

export async function stopGroup(groupId: number): Promise<void> {
  await http(`/api/groups/stop/${groupId}`, { method: 'POST' })
}
