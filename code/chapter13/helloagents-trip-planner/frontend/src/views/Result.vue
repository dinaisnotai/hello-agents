<template>
  <main class="result-page">
    <header class="toolbar">
      <a-button @click="goBack">返回规划</a-button>
      <a-space>
        <a-button @click="recalculate" :loading="replanning">重新计算约束</a-button>
        <a-button type="primary" @click="exportJson">导出 JSON</a-button>
      </a-space>
    </header>

    <a-empty v-if="!tripPlan" description="暂无旅行计划">
      <a-button type="primary" @click="goBack">创建行程</a-button>
    </a-empty>

    <section v-else class="content">
      <div class="hero">
        <div>
          <h1>{{ tripPlan.city }}旅行计划</h1>
          <p>{{ tripPlan.start_date }} 至 {{ tripPlan.end_date }}</p>
          <p>{{ tripPlan.overall_suggestions }}</p>
        </div>
        <a-progress
          type="circle"
          :percent="constraintPercent"
          :status="tripPlan.constraint_report?.passed ? 'success' : 'exception'"
        />
      </div>

      <div class="workspace">
        <div class="plan-column">
      <a-alert
        v-if="tripPlan.failure_reason"
        type="error"
        show-icon
        message="行程已生成，部分要求尚未满足"
        :description="failureDescription"
        class="warning"
      />

      <a-row :gutter="16">
        <a-col :xs="24" :md="8">
          <a-card title="预算" :bordered="false">
            <a-statistic title="预计总费用" :value="tripPlan.budget?.total || 0" suffix="元" />
            <p>全团规划估算，非实时成交价；门票、餐费按人数，住宿按房间和晚数计算。</p>
            <p v-if="tripPlan.budget?.unpriced_attractions?.length" style="color: #ad6800">预算尚未包含以下景点的门票：{{ tripPlan.budget.unpriced_attractions.join('、') }}。当前金额不是完整总费用。</p>
            <p>住宿 {{ tripPlan.budget?.total_hotels || 0 }} · 交通 {{ tripPlan.budget?.total_transportation || 0 }} · 餐饮 {{ tripPlan.budget?.total_meals || 0 }} · 门票 {{ tripPlan.budget?.total_attractions || 0 }} 元</p>
            <p v-if="tripPlan.budget?.budget_limit">预算上限：{{ tripPlan.budget.budget_limit }} 元</p>
            <p v-if="tripPlan.budget?.remaining !== undefined">
              剩余/超出：{{ tripPlan.budget.remaining }} 元
            </p>
          </a-card>
        </a-col>
        <a-col :xs="24" :md="8">
          <a-card title="路线" :bordered="false">
            <a-statistic title="总路线距离" :value="totalDistanceKm" suffix="km" />
            <p>路线段：{{ tripPlan.route_segments?.length || 0 }} 段</p>
          </a-card>
        </a-col>
        <a-col :xs="24" :md="8">
          <a-card title="行程" :bordered="false">
            <a-statistic title="旅行天数" :value="tripPlan.days.length" suffix="天" />
            <p>每天的游览、交通和休息时间见下方行程</p>
          </a-card>
        </a-col>
      </a-row>

      <a-card title="约束报告" :bordered="false">
        <a-list :data-source="tripPlan.constraint_report?.items || []">
          <template #renderItem="{ item }">
            <a-list-item>
              <a-tag :color="item.passed ? 'green' : item.severity === 'blocker' ? 'red' : 'orange'">
                {{ item.passed ? '通过' : '需调整' }}
              </a-tag>
              <div class="constraint-text">
                <strong>{{ item.name }}</strong>
                <span>{{ item.message }}</span>
                <small>{{ item.actual }} / {{ item.expected }}</small>
              </div>
            </a-list-item>
          </template>
        </a-list>
      </a-card>

      <a-alert
        v-for="warning in tripPlan.risk_warnings"
        :key="warning"
        type="warning"
        show-icon
        :message="warning"
        class="warning"
      />

      <a-card title="每日行程" :bordered="false">
        <a-collapse v-model:activeKey="activeDays">
          <a-collapse-panel v-for="day in tripPlan.days" :key="day.day_index" :header="dayTitle(day)">
            <a-descriptions :column="2" size="small" bordered>
              <a-descriptions-item label="交通">{{ day.transportation }}</a-descriptions-item>
              <a-descriptions-item label="住宿">{{ day.hotel?.name || day.accommodation }}</a-descriptions-item>
              <a-descriptions-item label="住宿等级">{{ day.hotel?.type || '等级待确认' }}（{{ day.hotel?.tier_source === 'map_category' ? '地图分类，星级待核实' : '以供应商确认信息为准' }}）</a-descriptions-item>
              <a-descriptions-item label="住宿估价">{{ day.hotel?.estimated_cost || 0 }} 元/间/晚 · 当天计 {{ day.accommodation_nights ?? 0 }} 晚；{{ day.hotel?.price_note || '非实时房价' }}</a-descriptions-item>
              <a-descriptions-item v-if="day.hotel?.quoted_total != null" label="供应商全住期金额">{{ day.hotel.quoted_total }} 元（全部房间）；{{ day.hotel.quote_checkin }} 至 {{ day.hotel.quote_checkout }} · {{ day.hotel.price_source === 'sandbox_quote' ? '沙箱测试，非真实可订报价' : '查询报价，预订前复核' }}</a-descriptions-item>
              <a-descriptions-item v-if="day.hotel?.star_rating" label="供应商星级">{{ day.hotel.star_rating }} 星 · 来源 {{ day.hotel.tier_source }}</a-descriptions-item>
              <a-descriptions-item label="交通估算">{{ day.daily_transport_cost || 0 }} 元
                <div v-for="(cost, label) in day.transport_fixed_costs" :key="label">{{ label }}：{{ cost }} 元</div>
              </a-descriptions-item>
              <a-descriptions-item label="交通总里程">{{ day.daily_distance_km || 0 }} km</a-descriptions-item>
              <a-descriptions-item label="路线步行">{{ day.daily_walking_distance_km || 0 }} km</a-descriptions-item>
              <a-descriptions-item label="预计总步行">{{ totalWalkingKm(day) }} km（含景点内）<span v-if="day.route_segments?.some(segment => segment.access_walking_confirmed === false)">；仅为已计入部分，上下车接驳步行待核实</span></a-descriptions-item>
              <a-descriptions-item label="游览时间">{{ formatMinutes(day.daily_visit_minutes) }}</a-descriptions-item>
              <a-descriptions-item label="交通时间">{{ formatMinutes(day.daily_travel_minutes) }}</a-descriptions-item>
              <a-descriptions-item label="餐饮及缓冲">{{ formatMinutes(day.daily_buffer_minutes) }}</a-descriptions-item>
              <a-descriptions-item label="全天合计">{{ formatMinutes(day.daily_duration_minutes) }}</a-descriptions-item>
              <a-descriptions-item label="计划时段">{{ day.planned_start_time || '--:--' }} - {{ day.planned_end_time || '--:--' }}</a-descriptions-item>
              <a-descriptions-item label="当日费用">{{ day.daily_cost || 0 }} 元</a-descriptions-item>
            </a-descriptions>

            <a-divider orientation="left">景点</a-divider>
            <a-list :data-source="day.attractions" bordered>
              <template #renderItem="{ item, index }">
                <a-list-item class="attraction-row">
                  <template #actions>
                    <a-button size="small" :disabled="index === 0" @click="moveAttraction(day.day_index, index, -1)">上移</a-button>
                    <a-button size="small" :disabled="index === day.attractions.length - 1" @click="moveAttraction(day.day_index, index, 1)">下移</a-button>
                  </template>
                  <div class="attraction-content">
                  <h3>{{ item.name }}</h3>
                  <p>{{ item.planned_arrival_time || '--:--' }} 到达 · {{ item.planned_departure_time || '--:--' }} 离开 · 游玩 {{ item.visit_duration }} 分钟</p>
                  <p v-if="item.visit_duration_note">{{ item.visit_duration_note }}</p>
                  <p>{{ item.address }}</p>
                  <p>{{ item.opening_time && item.closing_time ? `${item.hours_source === 'category_estimate' ? '预计营业' : '营业'} ${item.opening_time}–${item.closing_time}` : '营业时间待确认' }} · 门票 {{ item.ticket_price_status === 'unknown' || !item.ticket_price_status ? '待核实' : `${item.ticket_price}元（${item.ticket_price_status === 'estimate' ? '估价' : '参考价'}）` }}</p>
                  <p v-if="item.ticket_price_note">{{ item.ticket_price_note }} <a v-if="item.ticket_price_source" :href="item.ticket_price_source" target="_blank" rel="noopener noreferrer">票务来源</a></p>
                  <p v-if="item.access_note">{{ item.access_note }}</p>
                  </div>
                </a-list-item>
              </template>
            </a-list>

            <a-divider orientation="left">用餐安排</a-divider>
            <a-list :data-source="day.meals" bordered>
              <template #renderItem="{ item }">
                <a-list-item>
                  <a-list-item-meta :title="`${item.type === 'lunch' ? '午餐' : item.type === 'dinner' ? '晚餐' : '早餐'} · ${item.name}`"
                    :description="`${item.planned_arrival_time || '时间待确认'}—${item.planned_departure_time || ''} · 预留${item.duration_minutes || 0}分钟 · 估算${item.estimated_cost || 0}元/人 · ${item.source === 'map_poi' ? '地图餐厅' : '餐厅待确认'}；${item.description || ''}`" />
                  <a-tag v-if="item.cuisine_hint">本地特色方向：{{ item.cuisine_hint }}</a-tag>
                </a-list-item>
              </template>
            </a-list>
            <template v-if="day.schedule_blocks?.length">
              <a-divider orientation="left">休息与时间缓冲</a-divider>
              <a-list :data-source="day.schedule_blocks" bordered size="small">
                <template #renderItem="{ item }">
                  <a-list-item>
                    <a-list-item-meta
                      :title="`${item.type === 'rest' ? '休息' : '时间缓冲'} · ${item.start_time}—${item.end_time}`"
                      :description="item.reason || '已预留时间'"
                    />
                  </a-list-item>
                </template>
              </a-list>
            </template>
            <a-divider orientation="left">路线段（含已确认餐厅）</a-divider>
            <a-timeline>
              <a-timeline-item v-for="segment in day.route_segments" :key="`${segment.origin}-${segment.destination}`">
                <div v-if="segment.planned_departure_time || segment.planned_arrival_time" class="route-time">
                  {{ segment.planned_departure_time || '--:--' }} 出发 · {{ segment.planned_arrival_time || '--:--' }} 到达
                </div>
                {{ segment.origin }} → {{ segment.destination }}，
                {{ routeTypeLabel(segment.route_type) }}，
                {{ (segment.distance_meters / 1000).toFixed(1) }} km，
                共 {{ segment.duration_minutes }} 分钟
                <template
                  v-if="
                    segment.route_type === 'transit' &&
                    (segment.steps?.length ||
                      segment.transit_duration_minutes ||
                      segment.walking_duration_minutes)
                  "
                >
                  （公交/地铁 {{ segment.transit_duration_minutes || 0 }} 分钟，
                  步行 {{ segment.walking_duration_minutes || 0 }} 分钟 / {{ ((segment.walking_distance_meters || 0) / 1000).toFixed(1) }} km）
                </template>
                <div v-if="segment.description" class="route-description">{{ segment.description }}</div>
                <div v-if="segment.cost_note">费用估算 {{ segment.estimated_cost }} 元（参考 {{ segment.cost_low }}–{{ segment.cost_high }} 元）。{{ segment.cost_note }}</div>
                <ul v-if="segment.steps?.length" class="route-steps">
                  <li v-for="(step, stepIndex) in segment.steps" :key="stepIndex">
                    {{ routeTypeLabel(step.mode) }}
                    <template v-if="step.name"> · {{ step.name }}</template>
                    <template v-if="step.origin || step.destination"> · {{ step.origin }} → {{ step.destination }}</template>
                    · {{ step.duration_minutes }} 分钟
                  </li>
                </ul>
              </a-timeline-item>
            </a-timeline>
          </a-collapse-panel>
        </a-collapse>
      </a-card>

      <a-card v-if="tripPlan.evidence_sources?.length" title="参考攻略" :bordered="false">
        <a-list :data-source="tripPlan.evidence_sources">
          <template #renderItem="{ item }">
            <a-list-item>
              <a-list-item-meta :title="item.title" :description="item.snippet" />
              <a-tag v-if="item.retrieval_purpose">用于{{ item.retrieval_purpose }}规划</a-tag>
              <a-tag>{{ item.source }}</a-tag>
            </a-list-item>
          </template>
        </a-list>
      </a-card>
        </div>

        <aside v-if="sessionId" class="chat-column">
          <TripChatPanel
            :messages="chatMessages"
            :version="planVersion"
            :loading="chatLoading"
            @send="sendMessage"
          />
        </aside>
      </div>
    </section>
  </main>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import TripChatPanel from '@/components/TripChatPanel.vue'
import { getTripSession, replanTrip, replanTripSession, sendTripMessage } from '@/services/api'
import type { ConversationMessage, DayPlan, TripFormData, TripPlan } from '@/types'

const router = useRouter()
const route = useRoute()

const routeTypeLabel = (type: string) => ({
  walking: '步行',
  transit: '公共交通',
  subway: '地铁',
  bus: '公交',
  railway: '铁路',
  driving: '驾车',
} as Record<string, string>)[type] || type
const tripPlan = ref<TripPlan | null>(null)
const tripRequest = ref<TripFormData | undefined>()
const activeDays = ref<number[]>([0])
const replanning = ref(false)
const sessionId = ref('')
const chatMessages = ref<ConversationMessage[]>([])
const planVersion = ref(1)
const chatLoading = ref(false)

onMounted(async () => {
  const querySession = typeof route.query.session === 'string' ? route.query.session : ''
  sessionId.value = querySession || localStorage.getItem('currentTripSessionId') || ''
  if (sessionId.value) {
    try {
      const response = await getTripSession(sessionId.value)
      tripPlan.value = response.data.plan
      tripRequest.value = response.data.request
      chatMessages.value = response.data.messages
      planVersion.value = response.data.current_version
      sessionStorage.setItem('tripPlan', JSON.stringify(response.data.plan))
      sessionStorage.setItem('tripRequest', JSON.stringify(response.data.request))
      localStorage.setItem('currentTripSessionId', sessionId.value)
      if (!querySession) {
        router.replace({ path: '/result', query: { session: sessionId.value } })
      }
      return
    } catch (error: any) {
      message.warning(`${error.message}，已尝试读取本地缓存`)
    }
  }
  const plan = sessionStorage.getItem('tripPlan')
  const request = sessionStorage.getItem('tripRequest')
  if (plan) tripPlan.value = JSON.parse(plan)
  if (request) tripRequest.value = JSON.parse(request)
})

const constraintPercent = computed(() => Math.round((tripPlan.value?.constraint_report?.score || 0) * 100))
const failureDescription = computed(() => {
  const violations = tripPlan.value?.validation_result?.violations || []
  const reasons = [...new Set((tripPlan.value?.failure_reason || '').split(/[;；]/).map(item => item.trim()).filter(Boolean))]
  const onlyWalking = violations.length > 0 && violations.every(item => item.type === 'WALKING_LIMIT')
  if (onlyWalking) {
    return [...new Set(violations.map(item => item.message))].join('；') + '。景点安排可以保留作参考，但目前超过你设置的步行上限；总步行包含交通接驳和景点内部估算，修改交通方式或步行上限后需要重新计算。'
  }
  return reasons.join('；')
})
const totalDistanceKm = computed(() => {
  const meters = tripPlan.value?.route_segments?.reduce((sum, item) => sum + item.distance_meters, 0) || 0
  return Number((meters / 1000).toFixed(1))
})

const goBack = () => router.push('/')

const formatMinutes = (minutes = 0) => {
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return hours ? `${hours}小时${rest ? `${rest}分钟` : ''}` : `${rest}分钟`
}

const totalWalkingKm = (day: DayPlan) => Number((
  (day.daily_walking_distance_km || 0) +
  day.attractions.reduce((sum, item) => sum + (item.estimated_internal_walking_km || 0), 0)
).toFixed(2))

const dayTitle = (day: DayPlan) => {
  return `第 ${day.day_index + 1} 天｜${day.date}｜${formatMinutes(day.daily_duration_minutes)}｜${day.daily_distance_km || 0} km｜${day.daily_cost || 0} 元`
}

const moveAttraction = (dayIndex: number, attrIndex: number, direction: -1 | 1) => {
  if (!tripPlan.value) return
  const day = tripPlan.value.days.find(item => item.day_index === dayIndex)
  if (!day) return
  const nextIndex = attrIndex + direction
  if (nextIndex < 0 || nextIndex >= day.attractions.length) return
  ;[day.attractions[attrIndex], day.attractions[nextIndex]] = [day.attractions[nextIndex], day.attractions[attrIndex]]
}

const recalculate = async () => {
  if (!tripPlan.value) return
  replanning.value = true
  try {
    const payload = {
      plan: tripPlan.value,
      request: tripRequest.value,
      notes: '用户在结果页调整后重新计算'
    }
    if (sessionId.value) {
      const response = await replanTripSession(sessionId.value, payload)
      tripPlan.value = response.data.plan
      tripRequest.value = response.data.request
      chatMessages.value = response.data.messages
      planVersion.value = response.data.current_version
      sessionStorage.setItem('tripPlan', JSON.stringify(response.data.plan))
      sessionStorage.setItem('tripRequest', JSON.stringify(response.data.request))
      message.success('路线、预算和约束报告已更新')
    } else {
      const response = await replanTrip(payload)
      if (response.success && response.data) {
        tripPlan.value = response.data
        sessionStorage.setItem('tripPlan', JSON.stringify(response.data))
        message.success('路线、预算和约束报告已更新')
      }
    }
  } catch (error: any) {
    message.error(error.message || '重规划失败')
  } finally {
    replanning.value = false
  }
}

const sendMessage = async (content: string) => {
  if (!sessionId.value) return
  chatLoading.value = true
  try {
    const response = await sendTripMessage(sessionId.value, content)
    tripPlan.value = response.data.plan
    tripRequest.value = response.data.request
    chatMessages.value = response.data.messages
    planVersion.value = response.data.current_version
    sessionStorage.setItem('tripPlan', JSON.stringify(response.data.plan))
    sessionStorage.setItem('tripRequest', JSON.stringify(response.data.request))
    message.success('行程已更新并保存新版本')
  } catch (error: any) {
    message.error(error.message || '修改行程失败')
  } finally {
    chatLoading.value = false
  }
}

const exportJson = () => {
  if (!tripPlan.value) return
  const blob = new Blob([JSON.stringify(tripPlan.value, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `${tripPlan.value.city}-trip-plan.json`
  link.click()
  URL.revokeObjectURL(url)
}
</script>

<style scoped>
.result-page {
  min-height: 100vh;
  background: #f5f7fb;
  padding: 24px;
}

.toolbar,
.content {
  max-width: 1180px;
  margin: 0 auto;
}

.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
}

.content {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.hero {
  display: flex;
  justify-content: space-between;
  gap: 24px;
  align-items: center;
  background: #fff;
  border-radius: 8px;
  padding: 24px;
  box-shadow: 0 12px 28px rgba(21, 32, 56, 0.08);
}

.hero h1 {
  margin: 0 0 8px;
  color: #172033;
}

.hero p {
  margin: 4px 0;
  color: #667085;
}

.workspace {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 330px;
  gap: 16px;
  align-items: start;
}

.plan-column {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 16px;
}

.chat-column {
  min-width: 0;
}

.constraint-text {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.attraction-content {
  flex: 1 1 0;
  min-width: 0;
  overflow-wrap: anywhere;
}
.attraction-content h3 { margin: 0 0 8px; font-size: 16px; }
.attraction-content p { margin: 4px 0; color: #667085; line-height: 1.6; }
:deep(.attraction-row) { align-items: flex-start; gap: 16px; }
:deep(.attraction-row .ant-list-item-action) { flex: 0 0 auto; margin-left: 0; }
@media (max-width: 600px) {
  .result-page { padding: 12px; }
  .hero { padding: 16px; flex-direction: column; align-items: flex-start; }
  :deep(.ant-card-body) { padding: 12px; }
  :deep(.ant-collapse-content-box) { padding: 12px; }
  :deep(.attraction-row) { flex-direction: column; padding: 12px; }
  .attraction-content { width: 100%; flex-basis: auto; }
  :deep(.attraction-row .ant-list-item-action) { align-self: flex-end; }
}

.constraint-text small {
  color: #667085;
}

.warning {
  border-radius: 8px;
}

:deep(.ant-card) {
  border-radius: 8px;
  box-shadow: 0 8px 20px rgba(21, 32, 56, 0.06);
}

@media (max-width: 991px) {
  .workspace {
    grid-template-columns: 1fr;
  }

  .chat-column {
    order: -1;
  }
}
</style>
