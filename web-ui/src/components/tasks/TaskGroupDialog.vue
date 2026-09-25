<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { Task, TaskGroup, TaskGroupCreate, TaskGroupUpdate } from '@/types/task.d.ts'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { Switch } from '@/components/ui/switch'
import { toast } from '@/components/ui/toast'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Layers, Pencil, Play, Plus, Trash2, X } from 'lucide-vue-next'

const props = defineProps<{
  open: boolean
  groups: TaskGroup[]
  tasks: Task[]
}>()

const emit = defineEmits<{
  (e: 'update:open', value: boolean): void
  (e: 'create', data: TaskGroupCreate): void
  (e: 'update', groupId: number, data: TaskGroupUpdate): void
  (e: 'remove', groupId: number): void
  (e: 'start', groupId: number): void
  (e: 'stop', groupId: number): void
}>()

const { t } = useI18n()

// 编辑态：null = 列表视图，'new' = 新建，数字 = 编辑指定组
const editing = ref<'new' | number | null>(null)
const formName = ref('')
const formCron = ref('')
const formMode = ref<'serial' | 'parallel'>('serial')
const formEnabled = ref(true)

const isEditing = computed(() => editing.value !== null)
const editingExisting = computed(() =>
  typeof editing.value === 'number' ? editing.value : null
)

const taskCountByGroup = computed(() => {
  const counts = new Map<number, number>()
  props.tasks.forEach((task) => {
    if (task.group_id != null) {
      counts.set(task.group_id, (counts.get(task.group_id) || 0) + 1)
    }
  })
  return counts
})

function startCreate() {
  editing.value = 'new'
  formName.value = ''
  formCron.value = ''
  formMode.value = 'serial'
  formEnabled.value = true
}

function startEdit(group: TaskGroup) {
  editing.value = group.id
  formName.value = group.name
  formCron.value = group.cron || ''
  formMode.value = group.execution_mode === 'parallel' ? 'parallel' : 'serial'
  formEnabled.value = group.enabled
}

function cancelEdit() {
  editing.value = null
}

function handleSubmit() {
  if (!formName.value.trim()) {
    toast({ title: t('tasks.groups.validation.nameRequired'), variant: 'destructive' })
    return
  }
  const payload = {
    name: formName.value.trim(),
    cron: formCron.value.trim() || null,
    execution_mode: formMode.value,
    enabled: formEnabled.value,
  }
  const existingId = editingExisting.value
  if (existingId != null) {
    emit('update', existingId, payload)
  } else {
    emit('create', payload)
  }
  editing.value = null
}

function handleToggleEnabled(group: TaskGroup, enabled: boolean) {
  emit('update', group.id, { enabled })
}

function handleDelete(group: TaskGroup) {
  const count = taskCountByGroup.value.get(group.id) || 0
  if (count > 0 && !window.confirm(t('tasks.groups.deleteConfirm', { name: group.name, count }))) {
    return
  }
  emit('remove', group.id)
}

watch(
  () => props.open,
  (open) => {
    if (!open) editing.value = null
  }
)
</script>

<template>
  <Dialog :open="props.open" @update:open="(v) => emit('update:open', v)">
    <DialogContent class="sm:max-w-[640px] max-h-[85vh] overflow-y-auto">
      <DialogHeader>
        <DialogTitle>{{ t('tasks.groups.title') }}</DialogTitle>
        <DialogDescription>{{ t('tasks.groups.description') }}</DialogDescription>
      </DialogHeader>

      <!-- 编辑/新建表单 -->
      <div v-if="isEditing" class="space-y-4 rounded-lg border border-slate-200 bg-slate-50/60 p-4">
        <div class="flex items-center justify-between">
          <h3 class="text-sm font-bold text-slate-700">
            {{ editingExisting != null ? t('tasks.groups.editTitle') : t('tasks.groups.createTitle') }}
          </h3>
          <Button size="icon" variant="ghost" class="h-7 w-7" @click="cancelEdit">
            <X class="h-4 w-4" />
          </Button>
        </div>
        <div class="grid gap-2">
          <Label for="group-name">{{ t('tasks.groups.name') }}</Label>
          <Input id="group-name" v-model="formName" :placeholder="t('tasks.groups.namePlaceholder')" />
        </div>
        <div class="grid gap-2">
          <Label for="group-cron">{{ t('tasks.groups.cron') }}</Label>
          <Input id="group-cron" v-model="formCron" :placeholder="t('tasks.groups.cronPlaceholder')" />
          <p class="text-xs text-gray-500">{{ t('tasks.groups.cronHint') }}</p>
        </div>
        <div class="grid gap-2">
          <Label>{{ t('tasks.groups.executionMode') }}</Label>
          <select
            v-model="formMode"
            class="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2"
          >
            <option value="serial">{{ t('tasks.groups.modeSerial') }}</option>
            <option value="parallel">{{ t('tasks.groups.modeParallel') }}</option>
          </select>
          <p class="text-xs text-gray-500">
            {{ formMode === 'serial' ? t('tasks.groups.modeSerialHint') : t('tasks.groups.modeParallelHint') }}
          </p>
        </div>
        <div class="flex items-center gap-2">
          <Switch id="group-enabled" v-model="formEnabled" />
          <Label for="group-enabled">{{ t('tasks.groups.enabled') }}</Label>
        </div>
        <DialogFooter>
          <Button variant="outline" @click="cancelEdit">{{ t('common.cancel') }}</Button>
          <Button @click="handleSubmit">{{ t('common.save') }}</Button>
        </DialogFooter>
      </div>

      <!-- 组列表 -->
      <div v-else class="space-y-3">
        <div v-if="groups.length === 0" class="flex min-h-28 flex-col items-center justify-center gap-2 text-slate-300">
          <Layers class="h-10 w-10 opacity-30" />
          <p class="text-sm font-bold">{{ t('tasks.groups.empty') }}</p>
        </div>
        <div
          v-for="group in groups"
          :key="group.id"
          class="rounded-xl border border-slate-200/70 bg-slate-50/60 p-3"
        >
          <div class="flex items-start justify-between gap-3">
            <div class="min-w-0">
              <div class="flex flex-wrap items-center gap-2">
                <span class="text-sm font-black text-slate-800">{{ group.name }}</span>
                <Badge
                  variant="outline"
                  :class="group.execution_mode === 'parallel'
                    ? 'border-none bg-blue-50 text-[10px] font-black text-blue-600'
                    : 'border-none bg-amber-50 text-[10px] font-black text-amber-600'"
                >
                  {{ group.execution_mode === 'parallel' ? t('tasks.groups.modeParallel') : t('tasks.groups.modeSerial') }}
                </Badge>
                <Badge v-if="!group.enabled" variant="outline" class="border-slate-200 bg-slate-50 text-[10px] font-black text-slate-400">
                  {{ t('common.disabled') }}
                </Badge>
              </div>
              <div class="mt-1.5 flex flex-wrap items-center gap-2 text-xs text-slate-500">
                <span class="inline-flex items-center gap-1 font-mono font-semibold">
                  {{ group.cron || t('tasks.groups.noCron') }}
                </span>
                <span>·</span>
                <span>{{ t('tasks.groups.memberCount', { count: taskCountByGroup.get(group.id) || 0 }) }}</span>
              </div>
            </div>
            <div class="flex shrink-0 items-center gap-1.5">
              <Switch
                :model-value="group.enabled"
                class="data-[state=checked]:bg-primary scale-90"
                @update:model-value="(val: boolean) => handleToggleEnabled(group, val)"
              />
              <Button
                size="icon"
                variant="ghost"
                class="h-8 w-8 rounded-full text-emerald-600 hover:bg-emerald-50"
                :title="t('tasks.groups.startNow')"
                @click="emit('start', group.id)"
              >
                <Play class="h-3.5 w-3.5" />
              </Button>
              <Button
                size="icon"
                variant="ghost"
                class="h-8 w-8 rounded-full text-slate-400 hover:text-primary hover:bg-primary/5"
                :title="t('common.edit')"
                @click="startEdit(group)"
              >
                <Pencil class="h-3.5 w-3.5" />
              </Button>
              <Button
                size="icon"
                variant="ghost"
                class="h-8 w-8 rounded-full text-slate-400 hover:text-rose-500 hover:bg-rose-50"
                :title="t('common.delete')"
                @click="handleDelete(group)"
              >
                <Trash2 class="h-3.5 w-3.5" />
              </Button>
            </div>
          </div>
        </div>

        <Button v-if="!isEditing" variant="outline" class="w-full" @click="startCreate">
          <Plus class="mr-1 h-4 w-4" />
          {{ t('tasks.groups.createAction') }}
        </Button>
      </div>
    </DialogContent>
  </Dialog>
</template>
