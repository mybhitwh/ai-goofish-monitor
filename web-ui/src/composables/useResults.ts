import { ref, reactive, watch, onMounted, computed } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import type { ResultInsights, ResultItem } from '@/types/result.d.ts'
import type { Task, TaskGroup } from '@/types/task.d.ts'
import * as resultsApi from '@/api/results'
import type { GetResultContentParams } from '@/api/results'
import { useWebSocket } from '@/composables/useWebSocket'
import * as tasksApi from '@/api/tasks'
import * as groupsApi from '@/api/groups'

// 合并视图哨兵值（历史遗留）：聚合所有任务的结果
export const ALL_FILES = '__all__'
// 任务组合并视图哨兵前缀：__group_{id}__ 聚合同组任务的结果
export const GROUP_PREFIX = '__group_'

export function groupValue(groupId: number): string {
  return `${GROUP_PREFIX}${groupId}__`
}

export function parseGroupValue(value: string | null | undefined): number | null {
  if (!value) return null
  const match = value.match(/^__group_(\d+)__$/)
  return match ? Number(match[1]) : null
}

export function isMergedValue(value: string | null | undefined): boolean {
  return value === ALL_FILES || parseGroupValue(value) !== null
}

export function useResults() {
  const { t } = useI18n()
  const route = useRoute()
  // State
  const files = ref<string[]>([])
  const selectedFile = ref<string | null>(null)
  const results = ref<ResultItem[]>([])
  const insights = ref<ResultInsights | null>(null)
  const totalItems = ref(0)
  const page = ref(1)
  const limit = ref(100)
  const blacklistKeywords = ref<string[]>([])
  const taskNameByKeyword = ref<Record<string, string>>({})
  const groups = ref<TaskGroup[]>([])
  const tasksInfo = ref<Task[]>([])
  const isFileOptionsReady = ref(false)
  const hasFetchedFiles = ref(false)
  const hasFetchedTasks = ref(false)
  const isSavingBlacklist = ref(false)
  const readyDelayMs = 200
  let readyTimer: ReturnType<typeof setTimeout> | null = null
  
  const STORAGE_KEY_FILTERS = 'resultFilters'

  function loadPersistedFilters(): Required<Omit<GetResultContentParams, 'page' | 'limit'>> {
    const defaults: Required<Omit<GetResultContentParams, 'page' | 'limit'>> = {
      recommended_only: false,
      ai_recommended_only: false,
      keyword_recommended_only: false,
      include_hidden: false,
      sort_by: 'crawl_time',
      sort_order: 'desc',
    }
    try {
      const saved = localStorage.getItem(STORAGE_KEY_FILTERS)
      if (saved) return { ...defaults, ...JSON.parse(saved) }
    } catch { /* ignore */ }
    return defaults
  }

  const filters = reactive<Required<Omit<GetResultContentParams, 'page' | 'limit'>>>(loadPersistedFilters())

  const isLoading = ref(false)
  const error = ref<Error | null>(null)
  const { on } = useWebSocket()

  function normalizeKeyword(value: string) {
    return value.trim().toLowerCase().replace(/\s+/g, '_')
  }

  function getKeywordFromFilename(filename: string) {
    return filename.replace(/_full_data\.jsonl$/i, '').toLowerCase()
  }

  // Methods
  async function fetchGroups() {
    try {
      groups.value = await groupsApi.getAllGroups()
    } catch (e) {
      if (e instanceof Error) error.value = e
    }
  }

  // 当前组哨兵值对应的文件集合是否非空（组内至少有一个任务出过结果）
  const groupOptionEntries = computed(() => {
    if (groups.value.length === 0) return []
    const availableKeywords = new Set(files.value.map((file) => getKeywordFromFilename(file)))
    return groups.value
      .filter((group) =>
        tasksInfo.value.some(
          (task) =>
            task.group_id === group.id &&
            availableKeywords.has(normalizeKeyword(task.keyword || ''))
        )
      )
      .map((group) => ({
        value: groupValue(group.id),
        taskName: '',
        label: t('results.filters.groupFiles', { name: group.name }),
      }))
  })

  async function fetchFiles() {
    try {
      const fileList = await resultsApi.getResultFiles()
      files.value = fileList
      // If a file is selected that no longer exists, reset it.
      // Otherwise, if nothing is selected, select the first file by default.
      const optionValues = new Set(fileOptions.value.map((option) => option.value))
      if (selectedFile.value && optionValues.has(selectedFile.value)) {
        return
      }

      const lastSelected = localStorage.getItem('lastSelectedResultFile')
      if (lastSelected && optionValues.has(lastSelected)) {
        selectedFile.value = lastSelected
        return
      }

      // 默认进入第一个任务组（或任务）视图
      selectedFile.value = fileOptions.value.length > 0 ? fileOptions.value[0].value : null
    } catch (e) {
      if (e instanceof Error) error.value = e
    } finally {
      hasFetchedFiles.value = true
      scheduleFileOptionsReady()
    }
  }

  function parsePriceValue(value: unknown): number {
    if (typeof value === 'number') return value
    const n = parseFloat(String(value ?? '').replace(/[^\d.]/g, ''))
    return Number.isFinite(n) ? n : 0
  }

  function parseTimeValue(value: unknown): number {
    if (!value) return 0
    return Date.parse(String(value).replace(' ', 'T')) || 0
  }

  function sortMerged(items: ResultItem[]): ResultItem[] {
    const dir = filters.sort_order === 'asc' ? 1 : -1
    const key = filters.sort_by
    return items.sort((a, b) => {
      let av = 0
      let bv = 0
      if (key === 'price') {
        av = parsePriceValue(a.商品信息?.['当前售价'])
        bv = parsePriceValue(b.商品信息?.['当前售价'])
      } else if (key === 'keyword_hit_count') {
        av = a.ai_analysis?.keyword_hit_count ?? 0
        bv = b.ai_analysis?.keyword_hit_count ?? 0
      } else if (key === 'publish_time') {
        av = parseTimeValue(a.商品信息?.['发布时间'])
        bv = parseTimeValue(b.商品信息?.['发布时间'])
      } else {
        av = parseTimeValue(a['爬取时间'])
        bv = parseTimeValue(b['爬取时间'])
      }
      return (av - bv) * dir
    })
  }

  function mergeAndDedupe(lists: ResultItem[][]): ResultItem[] {
    const byId = new Map<string, ResultItem>()
    for (const list of lists) {
      for (const item of list) {
        const id =
          item.商品信息?.['商品ID'] || item.商品信息?.['商品链接'] || JSON.stringify(item).slice(0, 128)
        const prev = byId.get(id)
        if (!prev) {
          byId.set(id, item)
          continue
        }
        // 同一商品被多个任务命中时，保留"推荐"优先的那条
        const prevRec = prev.ai_analysis?.is_recommended ? 1 : 0
        const curRec = item.ai_analysis?.is_recommended ? 1 : 0
        if (curRec > prevRec) byId.set(id, item)
      }
    }
    return [...byId.values()]
  }

  /** 给合并视图的每条商品打上来源文件标记，隐藏/恢复时据此路由到对应文件 */
  function tagSourceFile(items: ResultItem[], filename: string): ResultItem[] {
    return items.map((item) => ({ ...item, _source_file: filename }))
  }

  async function fetchResults() {
    if (!selectedFile.value) {
      results.value = []
      totalItems.value = 0
      return
    }

    isLoading.value = true
    error.value = null
    try {
      const groupId = parseGroupValue(selectedFile.value)
      if (selectedFile.value === ALL_FILES || groupId !== null) {
        // 合并视图：任务组视图只聚合组内任务；全量视图聚合所有任务
        let targetFiles = files.value
        if (groupId !== null) {
          const groupKeywords = new Set(
            tasksInfo.value
              .filter((task) => task.group_id === groupId)
              .map((task) => normalizeKeyword(task.keyword || ''))
          )
          targetFiles = files.value.filter((file) =>
            groupKeywords.has(getKeywordFromFilename(file))
          )
        }
        const params = { ...filters, page: 1, limit: 100 }
        const responses = await Promise.all(
          targetFiles.map((file) =>
            resultsApi.getResultContent(file, params).catch(() => ({ total_items: 0, items: [] as ResultItem[] }))
          )
        )
        const merged = responses.map((response, index) => tagSourceFile(response.items, targetFiles[index]))
        results.value = sortMerged(mergeAndDedupe(merged))
        totalItems.value = results.value.length
        return
      }
      const data = await resultsApi.getResultContent(selectedFile.value, {
        ...filters,
        page: page.value,
        limit: limit.value,
      })
      results.value = data.items
      totalItems.value = data.total_items
    } catch (e) {
      if (e instanceof Error) error.value = e
      results.value = []
      totalItems.value = 0
    } finally {
      isLoading.value = false
    }
  }

  async function fetchInsights() {
    if (!selectedFile.value || isMergedValue(selectedFile.value)) {
      insights.value = null
      return
    }

    try {
      insights.value = await resultsApi.getResultInsights(selectedFile.value)
    } catch (e) {
      if (e instanceof Error) error.value = e
      insights.value = null
    }
  }

  async function fetchBlacklistRules() {
    if (!selectedFile.value || isMergedValue(selectedFile.value)) {
      blacklistKeywords.value = []
      return
    }

    try {
      const data = await resultsApi.getResultBlacklistRules(selectedFile.value)
      blacklistKeywords.value = data.keywords || []
    } catch (e) {
      if (e instanceof Error) error.value = e
      blacklistKeywords.value = []
    }
  }

  async function fetchTaskNameMap() {
    try {
      const tasks = await tasksApi.getAllTasks()
      tasksInfo.value = tasks
      const mapping: Record<string, string> = {}
      tasks.forEach((task) => {
        if (task.keyword) {
          mapping[normalizeKeyword(task.keyword)] = task.task_name
        }
      })
      taskNameByKeyword.value = mapping
    } catch (e) {
      if (e instanceof Error) error.value = e
    } finally {
      hasFetchedTasks.value = true
      scheduleFileOptionsReady()
    }
  }

  function scheduleFileOptionsReady() {
    if (isFileOptionsReady.value || !hasFetchedFiles.value || !hasFetchedTasks.value) return
    if (readyTimer) return
    readyTimer = setTimeout(() => {
      isFileOptionsReady.value = true
      readyTimer = null
    }, readyDelayMs)
  }

  // Real-time updates
  on('results_updated', async () => {
    const oldFile = selectedFile.value
    await fetchFiles()
    // If the selected file remains the same, refresh its content (in case of append)
    // If it changed (e.g. from null to new file), the watcher will handle it.
    if (selectedFile.value && selectedFile.value === oldFile) {
      fetchResults()
      fetchInsights()
    }
  })

  on('tasks_updated', () => {
    fetchTaskNameMap()
    fetchGroups()
  })

  async function refreshResults() {
    const current = selectedFile.value
    await fetchFiles()
    if (selectedFile.value && selectedFile.value === current) {
      await fetchResults()
      await fetchInsights()
      await fetchBlacklistRules()
    }
  }

  function exportSelectedResults() {
    if (!selectedFile.value || isMergedValue(selectedFile.value)) return
    resultsApi.downloadResultExport(selectedFile.value, { ...filters })
  }

  async function deleteSelectedFile(filename?: string) {
    const target = filename || selectedFile.value
    if (!target) return
    isLoading.value = true
    error.value = null
    try {
      await resultsApi.deleteResultFile(target)
      if (selectedFile.value === target) {
        const lastSelected = localStorage.getItem('lastSelectedResultFile')
        if (lastSelected === target) {
          localStorage.removeItem('lastSelectedResultFile')
        }
      }
      await fetchFiles()
    } catch (e) {
      if (e instanceof Error) error.value = e
      throw e
    } finally {
      isLoading.value = false
    }
  }

  async function toggleItemBlock(item: ResultItem) {
    if (!selectedFile.value) return
    const itemId = item.商品信息?.商品ID
    if (!itemId) return
    // 合并视图下按来源文件路由；单任务视图直接用当前选中文件
    const targetFile = isMergedValue(selectedFile.value) ? item._source_file : selectedFile.value
    if (!targetFile) return
    const newStatus = item._status === 'hidden' ? 'active' : 'hidden'
    try {
      await resultsApi.updateItemStatus(targetFile, itemId, newStatus)
      await fetchResults()
    } catch (e) {
      if (e instanceof Error) error.value = e
    }
  }

  async function saveBlacklistRules(keywords: string[]) {
    if (!selectedFile.value || isMergedValue(selectedFile.value)) return
    isSavingBlacklist.value = true
    error.value = null
    try {
      const data = await resultsApi.updateResultBlacklistRules(selectedFile.value, keywords)
      blacklistKeywords.value = data.keywords || []
      await fetchResults()
      await fetchInsights()
    } catch (e) {
      if (e instanceof Error) error.value = e
      throw e
    } finally {
      isSavingBlacklist.value = false
    }
  }

  // Watchers
  watch(filters, (val) => {
    localStorage.setItem(STORAGE_KEY_FILTERS, JSON.stringify(val))
  }, { deep: true })
  watch([selectedFile, filters], fetchResults, { deep: true })
  watch(selectedFile, () => {
    fetchInsights()
    fetchBlacklistRules()
  })
  watch(selectedFile, (value) => {
    if (value) localStorage.setItem('lastSelectedResultFile', value)
  })
  watch(
    [() => route.query.file, files],
    ([routeFile, currentFiles]) => {
      if (typeof routeFile !== 'string') return
      if (currentFiles.includes(routeFile)) {
        selectedFile.value = routeFile
      }
    },
    { immediate: true }
  )

  const fileOptions = computed(() => {
    const perTask = files.value.map((file) => {
      const keyword = getKeywordFromFilename(file)
      const taskName = taskNameByKeyword.value[keyword]
      return {
        value: file,
        taskName: taskName || t('common.unnamed'),
        label: t('results.filters.taskNameLabel', {
          task: taskName || t('common.unnamed'),
        }),
      }
    })
    // 任务组合并视图排在最前，随后是单个任务
    return [...groupOptionEntries.value, ...perTask]
  })

  // 当前是否处于合并视图（任务组或历史遗留的全量视图）
  const isMergedMode = computed(() => isMergedValue(selectedFile.value))

  // Lifecycle
  onMounted(() => {
    fetchFiles()
    fetchTaskNameMap()
    fetchGroups()
  })

  return {
    files,
    selectedFile,
    results,
    insights,
    totalItems,
    filters,
    isLoading,
    error,
    fetchFiles, // Expose to allow manual refresh
    refreshResults,
    exportSelectedResults,
    deleteSelectedFile,
    toggleItemBlock,
    blacklistKeywords,
    isSavingBlacklist,
    saveBlacklistRules,
    fileOptions,
    isFileOptionsReady,
    isMergedMode,
  }
}
