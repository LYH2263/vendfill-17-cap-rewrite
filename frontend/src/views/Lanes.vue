<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../api'
const rows = ref<any[]>([])
const refill = ref<any>(null)
const savingId = ref<number | null>(null)
const error = ref('')
const edits = ref<Record<number, string>>({})

async function loadAll() {
  rows.value = await api('/lanes?location_id=1')
  refill.value = await api('/refills/latest?location_id=1')
}

onMounted(loadAll)

async function saveCapacity(r: any) {
  error.value = ''
  const raw = (edits.value[r.id] ?? String(r.capacity)).trim()
  const cap = Number(raw)
  if (!Number.isInteger(cap) || cap <= 0) {
    error.value = `${r.slot_no} 容量必须为正整数，已取消（四处保持改前）`
    edits.value[r.id] = String(r.capacity)
    return
  }
  savingId.value = r.id
  try {
    await api(`/lanes/${r.id}`, { method: 'PATCH', body: JSON.stringify({ capacity: cap }) })
    edits.value[r.id] = String(cap)
    // 成功：容量、有效单（含单行/汇总/满仓口径）一起刷新为新口径
    await loadAll()
  } catch (e: any) {
    // 任一环节失败：后端已整体回滚，前端重新拉取，四处仍显示改前
    error.value = `${r.slot_no} 保存失败，容量与补货单已全部回滚：${e.message || e}`
    delete edits.value[r.id]
    await loadAll()
  } finally {
    savingId.value = null
  }
}
</script>
<template>
  <h1>货道格子</h1>
  <p class="sub">机面货道网格 · 格内库存条 · 改容量保存后强制重算当前有效补货单（同成同败）</p>
  <p v-if="error" class="badge badge-bad" style="display:inline-block;margin:0 0 0.75rem">{{ error }}</p>
  <div class="vf-machine-layout">
    <div class="vf-slot-grid">
      <div v-for="r in rows" :key="r.id" class="vf-slot">
        <div class="vf-slot-no">{{ r.slot_no }}</div>
        <div class="vf-slot-sku">{{ r.sku_name }}</div>
        <div class="vf-slot-bar">
          <div
            class="vf-slot-fill"
            :class="{ 'vf-need': r.gap > 0 }"
            :style="{ width: Math.min(r.fill_pct, 100) + '%' }"
          />
        </div>
        <div class="vf-slot-meta">{{ r.stock }}/{{ r.capacity }} · 缺 {{ r.gap }}</div>
        <div class="vf-slot-edit">
          容量
          <input
            v-model="edits[r.id]"
            :placeholder="String(r.capacity)"
            type="number"
            min="1"
            step="1"
            :disabled="savingId === r.id"
            @keyup.enter="saveCapacity(r)"
          />
          <button class="btn" :disabled="savingId === r.id" @click="saveCapacity(r)">
            {{ savingId === r.id ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>
    <aside class="vf-receipt" v-if="refill">
      <h2>*** 当前有效补货单 #{{ refill.id }} ***</h2>
      <div class="vf-receipt-line" v-for="l in refill.lines" :key="l.lane_id">
        <span>{{ l.slot_no }} {{ l.sku_name }}
          <small>({{ l.status === 'need_fill' ? '待补' : l.status === 'full' ? '满仓' : '超占' }})</small>
        </span>
        <span>x{{ l.fill_qty }}</span>
      </div>
      <p class="muted" style="margin:0.75rem 0 0;font-size:0.72rem;color:#6a5e48;text-align:center">
        合计补 {{ refill.total_fill }} · 满仓 {{ refill.full_count }} 道
      </p>
    </aside>
  </div>
</template>
