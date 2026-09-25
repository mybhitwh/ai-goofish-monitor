<script setup lang="ts">
import { ref, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { ResultItem } from '@/types/result.d.ts'
import {
  Card,
  CardContent,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import Badge from '@/components/ui/badge/Badge.vue'
import { ExternalLink, TrendingUp, TrendingDown, Info, User, Clock, CheckCircle2, XCircle, AlertCircle, EyeOff, Eye, StickyNote, Tag } from 'lucide-vue-next'
import { formatDateTime } from '@/i18n'

interface Props {
  item: ResultItem
  /** 已用标签候选（最近使用优先），来自父组件缓存 */
  usedTags?: string[]
}

const props = defineProps<Props>()
const emit = defineEmits<{
  (e: 'toggle-block', item: ResultItem): void
  (e: 'block', item: ResultItem, reasonTags: string[]): void
  (e: 'annotate', item: ResultItem, payload: { note?: string; tags?: string[] }): void
}>()
const { t } = useI18n()

const info = props.item.商品信息
const seller = props.item.卖家信息
const ai = props.item.ai_analysis
const priceInsight = props.item.price_insight

const isRecommended = ai?.is_recommended === true
const recommendationStatus = computed(() => {
  if (ai?.is_recommended === true) return { label: t('results.card.strongRecommend'), color: 'bg-emerald-500', icon: CheckCircle2, text: 'text-emerald-600', bg: 'bg-emerald-50' }
  if (ai?.is_recommended === false) return { label: t('results.card.notRecommended'), color: 'bg-rose-500', icon: XCircle, text: 'text-rose-600', bg: 'bg-rose-50' }
  return { label: t('results.card.pending'), color: 'bg-amber-500', icon: AlertCircle, text: 'text-amber-600', bg: 'bg-amber-50' }
})

const imageUrl = info.商品图片列表?.[0] || info.商品主图链接 || ''
const crawlTime = props.item.爬取时间
  ? formatDateTime(props.item.爬取时间, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
  : t('common.unknown')
const matchScore = ai?.value_score ?? 0
const isHidden = computed(() => props.item._effective_hidden === true || props.item._status === 'hidden')
const isRuleHidden = computed(() => props.item._hidden_reason === 'rule')
const canToggleBlock = computed(() => props.item._hidden_reason !== 'rule' && props.item._hidden_reason !== 'expired')
const hiddenLabel = computed(() => {
  if (props.item._hidden_reason === 'rule') return t('results.card.blacklisted')
  if (props.item._hidden_reason === 'expired') return t('results.card.expired')
  return t('results.card.hidden')
})

const expanded = ref(false)

/* ---------- 我的标注：备注 + 标签 ---------- */
const userTags = computed(() => props.item._user_tags || [])
const note = computed(() => props.item._note || '')

const blockReasonPresets = computed<string[]>(() => [
  t('results.card.blockReasonUnmatched'),
  t('results.card.blockReasonSeller'),
  t('results.card.blockReasonUsage'),
  t('results.card.blockReasonShipping'),
  t('results.card.blockReasonPrice'),
])

const isNoteEditing = ref(false)
const noteDraft = ref('')
const isTagEditing = ref(false)
const tagInput = ref('')
const isBlockPicking = ref(false)
const pickedReasons = ref<string[]>([])

function startNoteEditing() {
  noteDraft.value = note.value
  isNoteEditing.value = true
}

function saveNote() {
  const next = noteDraft.value.trim()
  if (next === note.value) {
    isNoteEditing.value = false
    return
  }
  emit('annotate', props.item, { note: next })
  isNoteEditing.value = false
}

function cancelNoteEditing() {
  isNoteEditing.value = false
}

const tagCandidates = computed(() => {
  const selected = new Set(userTags.value)
  return (props.usedTags || []).filter((tag) => !selected.has(tag)).slice(0, 8)
})

function addTag(raw: string) {
  const text = raw.trim()
  if (!text || userTags.value.includes(text)) {
    tagInput.value = ''
    return
  }
  emit('annotate', props.item, { tags: [...userTags.value, text] })
  tagInput.value = ''
}

function removeTag(tag: string) {
  emit('annotate', props.item, { tags: userTags.value.filter((item) => item !== tag) })
}

function handleTagInputEnter() {
  addTag(tagInput.value)
}

/* ---------- 屏蔽理由 ---------- */
function requestBlock() {
  pickedReasons.value = []
  isBlockPicking.value = true
}

function togglePickedReason(reason: string) {
  pickedReasons.value = pickedReasons.value.includes(reason)
    ? pickedReasons.value.filter((item) => item !== reason)
    : [...pickedReasons.value, reason]
}

function confirmBlockWithReasons() {
  isBlockPicking.value = false
  emit('block', props.item, [...pickedReasons.value])
}

function confirmBlockOnly() {
  isBlockPicking.value = false
  emit('block', props.item, [])
}

function handleBlockButtonClick() {
  if (isHidden.value) {
    emit('toggle-block', props.item)
  } else {
    requestBlock()
  }
}
</script>

<template>
  <Card class="group flex flex-col h-full border-none shadow-glass hover:shadow-card-hover transition-all duration-300 rounded-2xl overflow-hidden bg-white/80 backdrop-blur-sm" :class="{ 'opacity-50': isHidden }">
    <!-- Image Header -->
    <div class="relative aspect-[4/3] overflow-hidden">
      <div class="absolute inset-0 bg-slate-200 animate-pulse" v-if="!imageUrl"></div>
      <img
        v-else
        :src="imageUrl"
        :alt="info.商品标题"
        class="w-full h-full object-cover transition-transform duration-500 group-hover:scale-110"
        loading="lazy"
      />
      <!-- Hidden overlay -->
      <div v-if="isHidden" class="absolute inset-0 bg-black/30 flex items-center justify-center">
        <span class="text-white/80 text-xs font-semibold uppercase tracking-wider">{{ hiddenLabel }}</span>
      </div>
      <!-- Overlays -->
      <div class="absolute top-3 left-3 flex gap-2">
        <Badge v-if="isRecommended && !isHidden" variant="default" class="bg-emerald-500/90 backdrop-blur-md border-none shadow-sm">
          {{ t('results.card.curated') }}
        </Badge>
        <Badge v-if="isRuleHidden" variant="secondary" class="bg-slate-900/75 text-white border-none backdrop-blur-md shadow-sm">
          {{ t('results.card.blacklisted') }}
        </Badge>
      </div>
      <div class="absolute top-3 right-3 flex gap-1.5">
        <button
          v-if="canToggleBlock"
          type="button"
          @click="handleBlockButtonClick"
          :aria-label="isHidden ? t('results.card.unblock') : t('results.card.block')"
          class="flex rounded-full bg-black/50 p-1.5 text-white backdrop-blur-md border border-white/25 shadow-md opacity-100 transition-opacity sm:opacity-0 sm:group-hover:opacity-100 sm:focus-visible:opacity-100 hover:bg-black/70"
        >
          <EyeOff v-if="!isHidden" class="w-4 h-4" />
          <Eye v-else class="w-4 h-4" />
        </button>
         <a
           :href="info.商品链接"
           target="_blank"
           rel="noopener noreferrer"
           :aria-label="t('results.card.detail')"
           class="flex rounded-full bg-black/50 p-1.5 text-white backdrop-blur-md border border-white/25 shadow-md opacity-100 transition-opacity sm:opacity-0 sm:group-hover:opacity-100 sm:focus-visible:opacity-100 hover:bg-black/70"
         >
            <ExternalLink class="w-4 h-4" />
         </a>
      </div>
    </div>

    <CardHeader class="p-4 pb-2">
      <div class="flex justify-between items-start gap-3">
        <CardTitle class="text-base font-semibold text-slate-800 line-clamp-2 leading-snug flex-grow h-10">
          <a :href="info.商品链接" target="_blank" rel="noopener noreferrer" class="hover:text-primary transition-colors">
            {{ info.商品标题 }}
          </a>
        </CardTitle>
      </div>
      <div class="flex items-baseline gap-1 mt-2">
        <span class="text-2xl font-bold text-rose-600 tracking-tight">{{ info.当前售价 }}</span>
        <span v-if="info['商品原价']" class="text-xs text-slate-400 line-through mb-1">{{ info['商品原价'] }}</span>
      </div>
    </CardHeader>

    <CardContent class="p-4 pt-2 flex-grow">
      <!-- AI Insight Section -->
      <div class="rounded-xl p-3 border border-slate-100" :class="recommendationStatus.bg">
        <div class="flex items-center justify-between mb-2">
          <div class="flex items-center gap-2">
            <component :is="recommendationStatus.icon" class="w-4 h-4" :class="recommendationStatus.text" />
            <span class="text-sm font-bold" :class="recommendationStatus.text">{{ recommendationStatus.label }}</span>
          </div>
          <div class="flex items-center gap-1">
             <span class="text-[10px] font-medium uppercase tracking-wider text-slate-400">AI Match</span>
             <span class="text-sm font-black" :class="recommendationStatus.text">{{ matchScore }}%</span>
          </div>
        </div>
        
        <div class="w-full h-1.5 bg-white/50 rounded-full overflow-hidden mb-3">
          <div 
            class="h-full transition-all duration-1000 ease-out rounded-full" 
            :class="recommendationStatus.color"
            :style="{ width: `${matchScore}%` }"
          ></div>
        </div>

        <p class="text-xs leading-relaxed text-slate-600" :class="{ 'line-clamp-2': !expanded }">
           {{ ai?.reason || t('results.card.analyzing') }}
        </p>
        
        <button
          type="button"
          v-if="ai?.reason && ai.reason.length > 50"
          @click="expanded = !expanded" 
          class="mt-1 text-[10px] font-bold uppercase text-primary/70 hover:text-primary transition-colors flex items-center gap-1"
        >
          {{ expanded ? t('results.card.collapse') : t('results.card.expand') }}
          <Info class="w-3 h-3" />
        </button>
      </div>

      <!-- Price Stats Grid -->
      <div v-if="priceInsight?.observation_count" class="mt-4 grid grid-cols-2 gap-3">
        <div class="bg-slate-50/50 p-2.5 rounded-xl border border-slate-100/50 group/stat">
          <div class="flex items-center gap-1.5 text-[10px] font-medium text-slate-400 mb-1">
            <TrendingUp class="w-3 h-3" /> {{ t('results.card.marketAvg') }}
          </div>
          <div class="text-sm font-bold text-slate-700">
            {{ priceInsight.market_avg_price ? `¥${priceInsight.market_avg_price}` : '—' }}
          </div>
        </div>
        <div class="bg-slate-50/50 p-2.5 rounded-xl border border-slate-100/50">
          <div class="flex items-center gap-1.5 text-[10px] font-medium text-slate-400 mb-1">
            <TrendingDown class="w-3 h-3" /> {{ t('results.card.historicalLow') }}
          </div>
          <div class="text-sm font-bold text-slate-700">
            {{ priceInsight.min_price ? `¥${priceInsight.min_price}` : '—' }}
          </div>
        </div>
      </div>
    </CardContent>

    <!-- 我的标注：标签 + 备注 -->
    <div class="px-4 pb-1 space-y-2">
      <!-- 屏蔽理由选择面板 -->
      <div v-if="isBlockPicking" class="rounded-xl bg-slate-50 border border-slate-200 p-3 space-y-2">
        <p class="text-xs font-semibold text-slate-500">{{ t('results.card.blockReasonTitle') }}</p>
        <div class="flex flex-wrap gap-1.5">
          <button
            v-for="reason in blockReasonPresets"
            :key="reason"
            type="button"
            @click="togglePickedReason(reason)"
            class="text-xs px-2.5 py-1 rounded-full border transition-colors"
            :class="pickedReasons.includes(reason)
              ? 'bg-rose-100 border-rose-300 text-rose-700'
              : 'bg-white border-slate-200 text-slate-600 hover:border-rose-300'"
          >
            {{ reason }}
          </button>
        </div>
        <div class="flex justify-end gap-2 pt-1">
          <button type="button" @click="isBlockPicking = false" class="text-xs text-slate-500 hover:text-slate-700 px-2 py-1">
            {{ t('common.cancel') }}
          </button>
          <button type="button" @click="confirmBlockOnly" class="text-xs px-3 py-1 rounded-lg border border-slate-200 text-slate-600 hover:bg-slate-100">
            {{ t('results.card.blockOnly') }}
          </button>
          <button type="button" @click="confirmBlockWithReasons" class="text-xs px-3 py-1 rounded-lg bg-rose-500 text-white hover:bg-rose-600">
            {{ t('results.card.blockAndRecord') }}
          </button>
        </div>
      </div>

      <!-- 标签行 -->
      <div v-if="userTags.length > 0 || isTagEditing || !isHidden" class="flex flex-wrap items-center gap-1.5">
        <span
          v-for="tag in userTags"
          :key="tag"
          class="inline-flex items-center gap-1 text-xs px-2.5 py-0.5 rounded-full bg-blue-50 text-blue-700 border border-blue-100"
        >
          {{ tag }}
          <button
            v-if="isTagEditing"
            type="button"
            @click="removeTag(tag)"
            class="text-blue-400 hover:text-blue-700"
            :aria-label="t('results.card.removeTag')"
          >×</button>
        </span>

        <button
          v-if="!isTagEditing"
          type="button"
          @click="isTagEditing = true"
          class="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full border border-dashed border-slate-300 text-slate-400 hover:border-blue-300 hover:text-blue-600 transition-colors"
        >
          <Tag class="w-3 h-3" /> {{ t('results.card.addTag') }}
        </button>
        <button
          v-else-if="userTags.length > 0"
          type="button"
          @click="isTagEditing = false"
          class="text-xs text-slate-400 hover:text-slate-600 px-1"
        >
          {{ t('results.card.doneTagging') }}
        </button>
      </div>

      <!-- 标签编辑面板 -->
      <div v-if="isTagEditing" class="rounded-xl bg-slate-50 border border-slate-200 p-2.5 space-y-2">
        <div v-if="tagCandidates.length > 0" class="flex flex-wrap gap-1.5">
          <button
            v-for="candidate in tagCandidates"
            :key="candidate"
            type="button"
            @click="addTag(candidate)"
            class="text-xs px-2 py-0.5 rounded-full border border-slate-200 bg-white text-slate-600 hover:border-blue-300 hover:text-blue-600 transition-colors"
          >
            + {{ candidate }}
          </button>
        </div>
        <input
          v-model="tagInput"
          type="text"
          @keydown.enter.prevent="handleTagInputEnter"
          :placeholder="t('results.card.tagPlaceholder')"
          class="w-full text-xs px-2.5 py-1.5 rounded-lg border border-slate-200 bg-white focus:outline-none focus:border-blue-400"
        />
      </div>

      <!-- 备注显示 -->
      <div v-if="note && !isNoteEditing" class="group/note relative rounded-lg bg-slate-50 border border-slate-100 px-3 py-2">
        <p class="text-xs leading-relaxed text-slate-600 line-clamp-2 pr-8">
          <StickyNote class="w-3 h-3 inline mr-1 -mt-0.5 text-amber-500" />{{ note }}
        </p>
        <button
          v-if="!isHidden"
          type="button"
          @click="startNoteEditing"
          class="absolute right-2 top-2 text-[10px] text-slate-400 hover:text-blue-600"
        >
          {{ t('results.card.editNote') }}
        </button>
      </div>

      <!-- 备注编辑 -->
      <div v-if="isNoteEditing">
        <textarea
          v-model="noteDraft"
          rows="2"
          @keydown.ctrl.enter.prevent="saveNote"
          @keydown.meta.enter.prevent="saveNote"
          @keydown.esc.prevent="cancelNoteEditing"
          :placeholder="t('results.card.notePlaceholder')"
          class="w-full text-xs leading-relaxed px-3 py-2 rounded-lg border border-slate-200 bg-white focus:outline-none focus:border-blue-400 resize-none"
        ></textarea>
        <div class="flex items-center justify-between">
          <span class="text-[10px] text-slate-400">{{ t('results.card.noteSaveHint') }}</span>
          <button type="button" @click="saveNote" class="text-xs px-3 py-1 rounded-lg bg-blue-500 text-white hover:bg-blue-600">
            {{ t('common.save') }}
          </button>
        </div>
      </div>

      <!-- 空态：写备注入口 -->
      <div v-if="!note && !isNoteEditing && !isBlockPicking && !isHidden" class="flex justify-end">
        <button
          type="button"
          @click="startNoteEditing"
          class="inline-flex items-center gap-1 text-[11px] text-slate-400 hover:text-blue-600 transition-colors"
        >
          <StickyNote class="w-3 h-3" /> {{ t('results.card.addNote') }}
        </button>
      </div>
    </div>

    <CardFooter class="px-4 py-3 bg-slate-50/30 border-t border-slate-100/60 flex items-center justify-between text-[10px]">
      <div class="flex items-center gap-3 text-slate-400">
        <div class="flex items-center gap-1">
          <User class="w-3 h-3" />
          <span class="truncate max-w-[60px]">{{ seller.卖家昵称 || info.卖家昵称 || t('results.card.anonymous') }}</span>
        </div>
        <div class="flex items-center gap-1">
          <Clock class="w-3 h-3" />
          <span>{{ crawlTime }}</span>
        </div>
      </div>
      <a :href="info.商品链接" target="_blank" rel="noopener noreferrer" class="flex items-center gap-1 text-primary font-bold hover:gap-1.5 transition-all">
        {{ t('results.card.detail') }} <ExternalLink class="w-3 h-3" />
      </a>
    </CardFooter>
  </Card>
</template>
