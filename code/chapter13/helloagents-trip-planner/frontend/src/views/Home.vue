<template>
  <main class="home-page">
    <section class="planner-shell">
      <div class="page-title">
        <h1>旅行规划助手</h1>
        <p>告诉我们想去哪里、玩几天和必去景点，按你的节奏安排每天的路线。</p>
      </div>

      <a-card :bordered="false" class="form-card">
        <a-form :model="formData" layout="vertical" @finish="handleSubmit">
          <a-row :gutter="16">
            <a-col :xs="24" :md="8">
              <a-form-item label="目的地城市" name="city" :rules="[{ required: true, message: '请输入城市' }]">
                <a-input v-model:value="formData.city" placeholder="北京" size="large" />
              </a-form-item>
            </a-col>
            <a-col :xs="24" :md="8">
              <a-form-item label="开始日期" name="start_date" :rules="[{ required: true, message: '请选择开始日期' }]">
                <a-date-picker v-model:value="formData.start_date" style="width: 100%" size="large" />
              </a-form-item>
            </a-col>
            <a-col :xs="24" :md="8">
              <a-form-item label="结束日期" name="end_date" :rules="[{ required: true, message: '请选择结束日期' }]">
                <a-date-picker v-model:value="formData.end_date" style="width: 100%" size="large" />
              </a-form-item>
            </a-col>
          </a-row>

          <a-row :gutter="16">
            <a-col :xs="24" :md="6">
              <a-form-item label="旅行天数">
                <a-input-number v-model:value="formData.travel_days" :min="1" :max="30" style="width: 100%" size="large" />
              </a-form-item>
            </a-col>
            <a-col :xs="24" :md="6">
              <a-form-item label="总预算上限">
                <a-input-number v-model:value="formData.budget_limit" :min="0" :step="100" style="width: 100%" size="large" addon-after="元" />
              </a-form-item>
            </a-col>
            <a-col :xs="24" :md="6">
              <a-form-item label="每日最大步行">
                <a-input-number v-model:value="formData.max_daily_walk_km" :min="0" :step="0.5" style="width: 100%" size="large" addon-after="km" />
              </a-form-item>
            </a-col>
            <a-col :xs="24" :md="6">
              <a-form-item label="行程节奏">
                <a-segmented v-model:value="formData.pace" :options="paceOptions" size="large" block />
              </a-form-item>
            </a-col>
          </a-row>

          <a-row :gutter="16">
            <a-col :xs="24" :md="8">
              <a-form-item label="交通方式">
                <a-select v-model:value="formData.transportation" size="large">
                  <a-select-option value="公共交通">公共交通</a-select-option>
                  <a-select-option value="步行">步行</a-select-option>
                  <a-select-option value="打车/网约车">打车／网约车</a-select-option>
                  <a-select-option value="自有车辆自驾">自有车辆自驾</a-select-option>
                  <a-select-option value="租车自驾">租车自驾</a-select-option>
                  <a-select-option value="混合">混合（公共交通＋打车）</a-select-option>
                </a-select>
              </a-form-item>
            </a-col>
            <a-col :xs="24" :md="8">
              <a-form-item label="住宿偏好">
                <a-select v-model:value="formData.accommodation" size="large">
                  <a-select-option value="经济型酒店">经济型酒店</a-select-option>
                  <a-select-option value="舒适型酒店">舒适型酒店</a-select-option>
                  <a-select-option value="豪华酒店">豪华酒店</a-select-option>
                  <a-select-option value="民宿">民宿</a-select-option>
                </a-select>
              </a-form-item>
            </a-col>
            <a-col :xs="24" :md="8">
              <a-form-item label="住宿区域">
                <a-input v-model:value="formData.hotel_area" placeholder="例如：东城区、地铁沿线" size="large" />
              </a-form-item>
            </a-col>
          </a-row>

          <a-row :gutter="16">
            <a-col :xs="24" :md="6">
              <a-form-item label="每日最早出发">
                <a-input v-model:value="formData.daily_start_time" type="time" size="large" />
              </a-form-item>
            </a-col>
            <a-col :xs="24" :md="6">
              <a-form-item label="每日最晚结束">
                <a-input v-model:value="formData.daily_end_time" type="time" size="large" />
              </a-form-item>
            </a-col>
          </a-row>

          <a-form-item label="旅行偏好">
            <a-checkbox-group v-model:value="formData.preferences" :options="preferenceOptions" />
          </a-form-item>

          <a-row :gutter="16">
            <a-col :xs="24" :md="8">
              <a-form-item label="出行人数（交通、门票和餐费按人数估算）">
                <a-input-number v-model:value="formData.party_size" :min="1" :max="20" />
              </a-form-item>
            </a-col>
            <a-col :xs="24" :md="8">
              <a-form-item label="酒店房间数（出发日入住，结束日退房）">
                <a-input-number v-model:value="formData.room_count" :min="1" :max="10" />
              </a-form-item>
            </a-col>
            <a-col v-if="formData.transportation === '租车自驾'" :xs="24" :md="8">
              <a-form-item label="租赁计费天数（不足24小时按一天；留空按行程天数）">
                <a-input-number v-model:value="formData.rental_days" :min="1" :max="31" />
              </a-form-item>
            </a-col>
          </a-row>

          <a-row :gutter="16">
            <a-col :xs="24" :md="8">
              <a-form-item label="必去景点">
                <a-select v-model:value="formData.must_visit" mode="tags" size="large" placeholder="输入后回车，如：故宫" />
              </a-form-item>
            </a-col>
            <a-col :xs="24" :md="8">
              <a-form-item label="避开类型">
                <a-select
                  v-model:value="formData.avoid_categories"
                  mode="multiple"
                  :options="avoidCategoryOptions"
                  size="large"
                  placeholder="请选择需要避开的类型"
                />
              </a-form-item>
            </a-col>
            <a-col :xs="24" :md="8">
              <a-form-item label="饮食限制">
                <a-select v-model:value="formData.dietary_restrictions" mode="tags" size="large" placeholder="如：不吃海鲜、少辣" />
              </a-form-item>
            </a-col>
          </a-row>

          <a-form-item label="额外要求">
            <a-textarea v-model:value="formData.free_text_input" :rows="3" placeholder="例如：带老人出行，希望少走路，雨天优先室内景点。" />
          </a-form-item>

          <a-button type="primary" html-type="submit" size="large" block :loading="loading">
            生成可评测旅行计划
          </a-button>
        </a-form>
      </a-card>
    </section>
  </main>
</template>

<script setup lang="ts">
import { reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import dayjs, { type Dayjs } from 'dayjs'
import { generateTripPlan } from '@/services/api'
import type { TripFormData } from '@/types'

const router = useRouter()
const loading = ref(false)

const paceOptions = [
  { label: '轻松', value: 'relaxed' },
  { label: '均衡', value: 'balanced' },
  { label: '紧凑', value: 'packed' }
]

const preferenceOptions = ['历史文化', '自然风光', '美食', '博物馆', '城市漫步', '亲子', '休闲']

const avoidCategoryOptions = [
  { label: '公园园林', value: 'park' },
  { label: '博物馆展馆', value: 'museum' },
  { label: '购物商场', value: 'shopping' },
  { label: '寺庙宗教场所', value: 'temple' },
  { label: '游乐园', value: 'amusement' },
  { label: '动物园和海洋馆', value: 'zoo' },
  { label: '自然风光', value: 'natural' },
  { label: '历史古迹', value: 'historic' }
]

type TripFormState = Omit<TripFormData, 'start_date' | 'end_date'> & {
  start_date: Dayjs | null
  end_date: Dayjs | null
}

const formData = reactive<TripFormState>({
  city: '北京',
  start_date: dayjs(),
  end_date: dayjs().add(2, 'day'),
  travel_days: 3,
  transportation: '公共交通',
  accommodation: '经济型酒店',
  preferences: ['历史文化'],
  free_text_input: '',
  budget_limit: 2500,
  party_size: 1,
  room_count: 1,
  rental_days: undefined,
  pace: 'balanced',
  must_visit: ['故宫'],
  avoid_categories: [],
  dietary_restrictions: [],
  max_daily_walk_km: 8,
  hotel_area: '',
  daily_start_time: '',
  daily_end_time: ''
})

watch([() => formData.start_date, () => formData.end_date], ([start, end]) => {
  if (!start || !end) return
  const days = end.diff(start, 'day') + 1
  if (days <= 0) {
    message.warning('结束日期不能早于开始日期')
    formData.end_date = null
    return
  }
  if (days > 30) {
    message.warning('旅行天数不能超过 30 天')
    formData.end_date = null
    return
  }
  formData.travel_days = days
})

const handleSubmit = async () => {
  if (!formData.city.trim()) {
    message.error('请输入目的地城市')
    return
  }
  if (!formData.start_date || !formData.end_date) {
    message.error('请选择出行日期')
    return
  }

  loading.value = true
  try {
    const payload: TripFormData = {
      ...formData,
      city: formData.city.trim(),
      start_date: formData.start_date.format('YYYY-MM-DD'),
      end_date: formData.end_date.format('YYYY-MM-DD')
    }
    const response = await generateTripPlan(payload)
    // A degraded response still contains a saved, reviewable plan.  Show it
    // instead of stranding the user on the form; the result page explains the
    // constraints that still need attention.
    if (response.data) {
      sessionStorage.setItem('tripPlan', JSON.stringify(response.data))
      sessionStorage.setItem('tripRequest', JSON.stringify(payload))
      if (response.session_id) {
        localStorage.setItem('currentTripSessionId', response.session_id)
      }
      if (response.success) {
        message.success('旅行计划生成成功')
      } else {
        message.warning('行程已生成，但仍有未满足的约束，请先查看结果页的红色提示')
      }
      router.push({
        path: '/result',
        query: response.session_id ? { session: response.session_id } : undefined
      })
    } else {
      message.error(response.message || '生成失败')
    }
  } catch (error: any) {
    message.error(error.message || '生成失败')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.home-page {
  min-height: 100vh;
  background: #f5f7fb;
  padding: 40px 20px;
}

.planner-shell {
  max-width: 1180px;
  margin: 0 auto;
}

.page-title {
  margin-bottom: 24px;
}

.page-title h1 {
  margin: 0 0 8px;
  font-size: 32px;
  color: #172033;
}

.page-title p {
  margin: 0;
  color: #667085;
  font-size: 16px;
}

.form-card {
  border-radius: 8px;
  box-shadow: 0 12px 28px rgba(21, 32, 56, 0.08);
}
</style>
