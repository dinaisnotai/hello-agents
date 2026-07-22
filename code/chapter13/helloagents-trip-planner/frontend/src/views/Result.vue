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

      <a-row :gutter="16">
        <a-col :xs="24" :md="8">
          <a-card title="预算" :bordered="false">
            <a-statistic title="预计总费用" :value="tripPlan.budget?.total || 0" suffix="元" />
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
          <a-card title="证据" :bordered="false">
            <a-statistic title="攻略引用" :value="tripPlan.evidence_sources?.length || 0" suffix="条" />
            <p>用于减少纯 LLM 编造和补充避坑建议</p>
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
              <a-descriptions-item label="当日距离">{{ day.daily_distance_km || 0 }} km</a-descriptions-item>
              <a-descriptions-item label="游览时间">{{ formatMinutes(day.daily_visit_minutes) }}</a-descriptions-item>
              <a-descriptions-item label="交通时间">{{ formatMinutes(day.daily_travel_minutes) }}</a-descriptions-item>
              <a-descriptions-item label="餐饮及缓冲">{{ formatMinutes(day.daily_buffer_minutes) }}</a-descriptions-item>
              <a-descriptions-item label="全天合计">{{ formatMinutes(day.daily_duration_minutes) }}</a-descriptions-item>
              <a-descriptions-item label="当日费用">{{ day.daily_cost || 0 }} 元</a-descriptions-item>
            </a-descriptions>

            <a-divider orientation="left">景点</a-divider>
            <a-list :data-source="day.attractions" bordered>
              <template #renderItem="{ item, index }">
                <a-list-item>
                  <template #actions>
                    <a-button size="small" :disabled="index === 0" @click="moveAttraction(day.day_index, index, -1)">上移</a-button>
                    <a-button size="small" :disabled="index === day.attractions.length - 1" @click="moveAttraction(day.day_index, index, 1)">下移</a-button>
                  </template>
                  <a-list-item-meta :title="item.name" :description="`${item.address}｜${item.visit_duration}分钟｜门票 ${item.ticket_price || 0}元`" />
                </a-list-item>
              </template>
            </a-list>

            <a-divider orientation="left">路线段</a-divider>
            <a-timeline>
              <a-timeline-item v-for="segment in day.route_segments" :key="`${segment.origin}-${segment.destination}`">
                {{ segment.origin }} → {{ segment.destination }}，
                {{ (segment.distance_meters / 1000).toFixed(1) }} km，
                {{ segment.duration_minutes }} 分钟
              </a-timeline-item>
            </a-timeline>
          </a-collapse-panel>
        </a-collapse>
      </a-card>

      <a-card v-if="tripPlan.evidence_sources?.length" title="RAG 攻略证据" :bordered="false">
        <a-list :data-source="tripPlan.evidence_sources">
          <template #renderItem="{ item }">
            <a-list-item>
              <a-list-item-meta :title="item.title" :description="item.snippet" />
              <a-tag>{{ item.source }}</a-tag>
            </a-list-item>
          </template>
        </a-list>
      </a-card>
    </section>
  </main>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { replanTrip } from '@/services/api'
import type { DayPlan, TripFormData, TripPlan } from '@/types'

const router = useRouter()
const tripPlan = ref<TripPlan | null>(null)
const tripRequest = ref<TripFormData | undefined>()
const activeDays = ref<number[]>([0])
const replanning = ref(false)

onMounted(() => {
  const plan = sessionStorage.getItem('tripPlan')
  const request = sessionStorage.getItem('tripRequest')
  if (plan) tripPlan.value = JSON.parse(plan)
  if (request) tripRequest.value = JSON.parse(request)
})

const constraintPercent = computed(() => Math.round((tripPlan.value?.constraint_report?.score || 0) * 100))
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
    const response = await replanTrip({
      plan: tripPlan.value,
      request: tripRequest.value,
      notes: '用户在结果页调整后重新计算'
    })
    if (response.success && response.data) {
      tripPlan.value = response.data
      sessionStorage.setItem('tripPlan', JSON.stringify(response.data))
      message.success('路线、预算和约束报告已更新')
    }
  } catch (error: any) {
    message.error(error.message || '重规划失败')
  } finally {
    replanning.value = false
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

.constraint-text {
  display: flex;
  flex-direction: column;
  gap: 4px;
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
</style>
