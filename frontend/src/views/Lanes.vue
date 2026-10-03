<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../api'
const rows = ref<any[]>([])
const refill = ref<any>(null)
const editing = ref<any>(null)
const editCapacity = ref<number>(1)
const saving = ref(false)
const error = ref('')

async function reload() {
  rows.value = await api('/lanes')
  try { refill.value = await api('/refills/latest?location_id=1') } catch { /* */ }
}
onMounted(reload)

function startEdit(r: any) {
  editing.value = r
  editCapacity.value = r.capacity
  error.value = ''
}
function cancelEdit() { editing.value = null }

async function saveCapacity() {
  const r = editing.value
  if (!r) return
  const cap = Number(editCapacity.value)
  if (!Number.isInteger(cap) || cap <= 0) { error.value = `${r.slot_no} 容量必须为正整数`; return }
  saving.value = true
  error.value = ''
  try {
    await api(`/lanes/${r.id}`, { method: 'PUT', body: JSON.stringify({ capacity: cap }) })
    editing.value = null
    await reload()
  } catch (e: any) {
    error.value = `${r.slot_no} 保存失败，容量与补货单保持原状：${e?.message ?? e}`
  } finally {
    saving.value = false
  }
}
</script>
<template>
  <h1>货道格子</h1>
  <p class="sub">机面货道网格 · 格内库存条 · 改容量保存后按新容量重算当前补货单</p>
  <p v-if="error" class="vf-error">{{ error }}</p>
  <div v-if="editing" class="card vf-editor">
    <strong>{{ editing.slot_no }} {{ editing.sku_name }}</strong>
    <span class="muted">库存 {{ editing.stock }} · 在途 {{ editing.in_transit }} · 当前容量 {{ editing.capacity }}</span>
    <label>新容量
      <input v-model.number="editCapacity" type="number" min="1" step="1" :disabled="saving" />
    </label>
    <button class="btn" :disabled="saving" @click="saveCapacity">保存</button>
    <button class="btn vf-btn-ghost" :disabled="saving" @click="cancelEdit">取消</button>
  </div>
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
        <button class="vf-slot-edit" @click="startEdit(r)">改容量</button>
      </div>
    </div>
    <aside class="vf-receipt" v-if="refill">
      <h2>*** 补货建议单 ***</h2>
      <div class="vf-receipt-line" v-for="l in refill.lines" :key="l.lane_id">
        <span>{{ l.slot_no }} {{ l.sku_name }}</span>
        <span>x{{ l.fill_qty }}</span>
      </div>
      <p class="muted" style="margin:0.75rem 0 0;font-size:0.72rem;color:#6a5e48;text-align:center">
        — 机面打印预览 —
      </p>
    </aside>
  </div>
</template>
