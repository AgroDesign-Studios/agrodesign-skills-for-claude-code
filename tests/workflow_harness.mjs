// Runs agrodesign-deep-review.workflow.js outside Claude Code with stand-ins for the Workflow
// runtime (agent, parallel, pipeline, log), driven by a scenario file:
//
//   node workflow_harness.mjs <workflow.js> <scenario.json>
//
// scenario = { args: {...}, rules: [{ label: "<regex>", model?: "<model>", value: {...} | null, reject?: "<message>" }] }
// The first rule whose label regex matches the call's label (and model, if given)
// answers it; "$HEAD" in a value is replaced by args.head. Prints one JSON object:
// { result, error, calls: [{label, model, effort, agentType, required, promptHead, hasHead, hasExtra}] }
import { readFileSync } from 'node:fs'

const [scriptPath, scenarioPath] = process.argv.slice(2)
const scenario = JSON.parse(readFileSync(scenarioPath, 'utf8'))
const source = readFileSync(scriptPath, 'utf8').replace('export const meta', 'const meta')
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const calls = []

function fill(value, head) {
  return value == null ? value : JSON.parse(JSON.stringify(value).replaceAll('$HEAD', head))
}

async function agent(prompt, opts) {
  const head = (scenario.args && scenario.args.head) || ''
  calls.push({
    label: opts.label, model: opts.model, effort: opts.effort, agentType: opts.agentType,
    required: (opts.schema && opts.schema.required) || [],
    promptHead: prompt.slice(0, 14), hasHead: prompt.includes(head),
    hasExtra: prompt.includes('PROJECT PROBE RULES'),
  })
  for (const rule of scenario.rules || []) {
    if (new RegExp(rule.label).test(opts.label) && (!rule.model || rule.model === opts.model)) {
      if (rule.reject) throw new Error(rule.reject)
      return fill(rule.value, head)
    }
  }
  return null
}
const parallel = (fns) => Promise.all(fns.map((f) => f()))
async function pipeline(items, ...stages) {
  return Promise.all(items.map(async (item) => {
    let value = item
    for (const stage of stages) {
      value = await stage(value, item)
      if (value == null) return null
    }
    return value
  }))
}
const log = () => {}

let result = null
let error = null
try {
  result = await new AsyncFunction('args', 'agent', 'parallel', 'pipeline', 'log', 'phase', source)(scenario.args, agent, parallel, pipeline, log, () => {})
} catch (e) {
  error = String(e && e.message || e)
}
process.stdout.write(JSON.stringify({ result, error, calls }))
