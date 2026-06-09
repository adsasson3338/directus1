{
  "name": "Qualify Sheet",
  "nodes": [
    {
      "parameters": {
        "httpMethod": "POST",
        "path": "qualify-sheet",
        "responseMode": "responseNode",
        "options": {}
      },
      "type": "n8n-nodes-base.webhook",
      "typeVersion": 2,
      "position": [-240, 0],
      "id": "webhook-qualify",
      "name": "Webhook"
    },
    {
      "parameters": {
        "respondWith": "json",
        "responseBody": "={{ JSON.stringify({\"status\": \"received\", \"job_id\": $json.job_id}) }}",
        "options": {}
      },
      "type": "n8n-nodes-base.respondToWebhook",
      "typeVersion": 1.1,
      "position": [-20, -120],
      "id": "respond-webhook",
      "name": "Respond to Webhook"
    },
    {
      "parameters": {
        "jsCode": "const body = $input.first().json;\nconst job_id = body.job_id;\nconst s = body.signals;\n\nconst prompt = `You are the gatekeeper of a retail sales data ingestion pipeline. A sheet has arrived and you must determine if it should be disqualified.\n\nA sheet should be disqualified if it does NOT contain unit sales data. Unit sales data has integer values at the intersection of product identifiers and date columns.\n\nDisqualify if any of the following are true:\n- Crosshair values are decimals above 1 — dollar revenue\n- Crosshair values are between 0 and 1 — percentage metrics\n- Column labels contain $$$ or $$ — dollar sheet\n- Sheet name contains CFP, Forecast, FCST, Projection, Order\n- Vocabulary contains forecast or projection language such as: Total DC Order Projection, Projected Store Orders, Collaborative FC, OOS % LY, Instock Goal, Projected Orders\n\nEVIDENCE:\nSheet name: ${s.sheet_name}\nFilename: ${s.filename}\nCrosshair sample values: ${JSON.stringify(s.crosshair_sample)}\nDominant crosshair type: ${s.dominant_type}\nColumn labels: ${JSON.stringify(s.column_labels)}\nInline strings: ${JSON.stringify(s.inline_strings)}\n\nBase all decisions on the evidence above. Do not guess.\nRespond with JSON only — no prose, no markdown:\n{\"disqualified\": true or false, \"reason\": \"one sentence explanation\"}`;\n\nreturn [{ json: { job_id, prompt } }];"
      },
      "type": "n8n-nodes-base.code",
      "typeVersion": 2,
      "position": [-20, 60],
      "id": "build-prompt",
      "name": "Build Prompt"
    },
    {
      "parameters": {
        "promptType": "define",
        "text": "={{ $json.prompt }}",
        "batching": {}
      },
      "type": "@n8n/n8n-nodes-langchain.chainLlm",
      "typeVersion": 1.9,
      "position": [220, 60],
      "id": "llm-chain",
      "name": "LLM Chain"
    },
    {
      "parameters": {
        "model": "z-ai/glm-4.5-air",
        "options": {
          "maxTokens": 300
        }
      },
      "type": "@n8n/n8n-nodes-langchain.lmChatOpenRouter",
      "typeVersion": 1,
      "position": [300, 240],
      "id": "openrouter-model",
      "name": "OpenRouter Chat Model",
      "credentials": {
        "openRouterApi": {
          "id": "A2TyYgieAAJY0zWe",
          "name": "OpenRouter sakar"
        }
      }
    },
    {
      "parameters": {
        "jsCode": "const job_id = $('Build Prompt').first().json.job_id;\nconst raw = $input.first().json.text || '';\n\n// Strip markdown fences if present\nconst clean = raw.replace(/^```json?\\n?/, '').replace(/\\n?```$/, '').trim();\n\nlet verdict;\ntry {\n  verdict = JSON.parse(clean);\n} catch(e) {\n  verdict = { disqualified: null, reason: 'Failed to parse AI response: ' + raw };\n}\n\nreturn [{ json: { job_id, verdict } }];"
      },
      "type": "n8n-nodes-base.code",
      "typeVersion": 2,
      "position": [460, 60],
      "id": "parse-response",
      "name": "Parse Response"
    },
    {
      "parameters": {
        "method": "POST",
        "url": "=http://discovery-parser:8000/response/{{ $json.job_id }}",
        "sendBody": true,
        "specifyBody": "json",
        "jsonBody": "={{ JSON.stringify($json.verdict) }}",
        "options": {}
      },
      "type": "n8n-nodes-base.httpRequest",
      "typeVersion": 4.4,
      "position": [700, 60],
      "id": "post-response",
      "name": "POST to Python"
    }
  ],
  "connections": {
    "Webhook": {
      "main": [
        [
          { "node": "Respond to Webhook", "type": "main", "index": 0 },
          { "node": "Build Prompt", "type": "main", "index": 0 }
        ]
      ]
    },
    "Build Prompt": {
      "main": [
        [{ "node": "LLM Chain", "type": "main", "index": 0 }]
      ]
    },
    "OpenRouter Chat Model": {
      "ai_languageModel": [
        [{ "node": "LLM Chain", "type": "ai_languageModel", "index": 0 }]
      ]
    },
    "LLM Chain": {
      "main": [
        [{ "node": "Parse Response", "type": "main", "index": 0 }]
      ]
    },
    "Parse Response": {
      "main": [
        [{ "node": "POST to Python", "type": "main", "index": 0 }]
      ]
    }
  },
  "active": false,
  "settings": {
    "executionOrder": "v1"
  },
  "meta": {
    "templateCredsSetupCompleted": true,
    "instanceId": "3b8e8ec4fc4043451bd31b5c2637cc70f884729057388b776dda6217241a5572"
  },
  "tags": []
}
