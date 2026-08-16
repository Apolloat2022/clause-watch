// ClauseWatch — everything inside the resource group.
//
// Split out of main.bicep only because main.bicep is subscription-scoped (azd's
// default) and resource-scoped resources cannot be declared there. This is the
// whole system: identity, data stores, the queue, Document Intelligence, the
// registry, the API Container App, and the two Container Apps Jobs.

targetScope = 'resourceGroup'

param location string
param tags object

@minLength(3)
@maxLength(12)
param appName string

@allowed(['dev', 'prod'])
param appEnvironment string

param environmentName string
param embeddingDimensions int
param containerImage string
param foundryResource string
param azureOpenAiEndpoint string
param promptVersion string

@secure()
param notifyWebhookUrl string

param entraTenantId string
param entraAudience string

param principalId string

@allowed(['User', 'ServicePrincipal'])
param principalType string

var suffix = uniqueString(resourceGroup().id, appName, environmentName)
var prefix = '${appName}-${appEnvironment}'

// ---------------------------------------------------------------- identity
// One user-assigned identity shared by the API and both jobs. Every data-plane
// call uses it, so there are no connection strings anywhere in the app.
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-id'
  location: location
  tags: tags
}

// ------------------------------------------------------------ observability
resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${prefix}-logs'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    // Default is 90; 30 is plenty for a portfolio project and cuts ingest cost.
    retentionInDays: 30
  }
}

// ----------------------------------------------------------------- storage
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  // Storage account names cap at 24 characters, which 'st' + appName + a full
  // 13-character uniqueString overflows. take() clips the tail of the suffix;
  // what remains is still derived from the resource group id and unique enough.
  name: take('st${toLower(appName)}${suffix}', 24)
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    // Managed identity only; shared-key auth is disabled outright so a leaked
    // key cannot exist to be misused.
    allowSharedKeyAccess: false
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource contractsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'contracts'
  properties: { publicAccess: 'None' }
}

// --------------------------------------------------------------- cosmos db
resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' = {
  name: 'cosmos-${appName}-${suffix}'
  location: location
  tags: tags
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    locations: [
      { locationName: location, failoverPriority: 0, isZoneRedundant: false }
    ]
    // Serverless: billed per request-unit consumed, nothing when idle.
    capabilities: [
      { name: 'EnableServerless' }
      // Required for vector indexing (DiskANN) in Cosmos NoSQL.
      { name: 'EnableNoSQLVectorSearch' }
    ]
    disableLocalAuth: true
    minimalTlsVersion: 'Tls12'
  }
}

resource cosmosDb 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-11-15' = {
  parent: cosmos
  name: 'clausewatch'
  properties: {
    resource: { id: 'clausewatch' }
  }
}

// contracts / obligations / audit — plain containers, partitioned by contract_id.
var plainContainers = [
  'contracts'
  'obligations'
  'audit'
]

resource containers 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = [
  for name in plainContainers: {
    parent: cosmosDb
    name: name
    properties: {
      resource: {
        id: name
        partitionKey: {
          // snake_case to match the Pydantic field name exactly, so documents
          // are stored as model_dump() with no mapping layer between the
          // domain model and the stored shape. See docs/DECISIONS.md 005.
          paths: ['/contract_id']
          kind: 'Hash'
        }
      }
    }
  }
]

// clauses — the vector-indexed container. This is what replaces Azure AI
// Search: DiskANN over the clause embeddings, queried with VectorDistance().
resource clausesContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = {
  parent: cosmosDb
  name: 'clauses'
  properties: {
    resource: {
      id: 'clauses'
      partitionKey: {
        paths: ['/contract_id']
        kind: 'Hash'
      }
      vectorEmbeddingPolicy: {
        vectorEmbeddings: [
          {
            path: '/embedding'
            dataType: 'float32'
            dimensions: embeddingDimensions
            distanceFunction: 'cosine'
          }
        ]
      }
      indexingPolicy: {
        indexingMode: 'consistent'
        // The embedding array must be excluded from the normal index — leaving
        // it in inflates RU cost badly and buys nothing; the vector index below
        // is what serves similarity queries.
        includedPaths: [{ path: '/*' }]
        excludedPaths: [{ path: '/embedding/*' }]
        vectorIndexes: [
          { path: '/embedding', type: 'diskANN' }
        ]
      }
    }
  }
}

// -------------------------------------------------------------- service bus
resource serviceBus 'Microsoft.ServiceBus/namespaces@2022-10-01-preview' = {
  name: 'sb-${appName}-${suffix}'
  location: location
  tags: tags
  sku: { name: 'Basic', tier: 'Basic' }
  properties: {
    disableLocalAuth: true
    minimumTlsVersion: '1.2'
  }
}

resource ingestQueue 'Microsoft.ServiceBus/namespaces/queues@2022-10-01-preview' = {
  parent: serviceBus
  name: 'contract-ingest'
  properties: {
    // A poison PDF dead-letters after 5 attempts rather than retrying forever.
    maxDeliveryCount: 5
    lockDuration: 'PT5M'
    deadLetteringOnMessageExpiration: true
  }
}

// ------------------------------------------------- document intelligence
resource docIntelligence 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: 'di-${appName}-${suffix}'
  location: location
  tags: tags
  kind: 'FormRecognizer'
  sku: { name: 'S0' }
  properties: {
    customSubDomainName: 'di-${appName}-${suffix}'
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: true
  }
}

// ------------------------------------------------------- container registry
// The one always-on charge in this template (~$5/month for Basic); there is no
// scale-to-zero registry tier. Admin user stays off — the API and both jobs
// pull with the shared managed identity and the AcrPull assignment below.
resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: take('cr${toLower(appName)}${suffix}', 50)
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false
    anonymousPullEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

// ------------------------------------------------------ container apps env
resource containerEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${prefix}-env'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

// --------------------------------------------------------------- the image
// azure.yaml pins docker.image and docker.tag, so the reference `azd deploy`
// pushes is deterministic rather than a timestamped tag nothing can predict.
var imageRepository = '${appName}/api'
var builtImage = '${containerRegistry.properties.loginServer}/${imageRepository}:latest'

// The API gets a public placeholder on the very first provision and nothing
// else: no image exists in the registry yet, and a Container App whose image
// cannot be pulled fails the deployment outright. `azd deploy` replaces it
// minutes later in the same `azd up`, so it never survives a run.
var bootstrapImage = 'mcr.microsoft.com/k8se/quickstart:latest'
var apiImage = empty(containerImage) ? bootstrapImage : containerImage

// The jobs deliberately do NOT get that placeholder. azd only ever updates the
// image on the resource tagged azd-service-name, so a job left pointing at the
// quickstart image would stay there permanently — running, reporting success,
// and draining nothing. Pointing at the real repository instead means the
// worst case is a loud ImagePullBackOff on the first execution rather than a
// silent no-op. Every provision after the first pins the exact built image.
var jobImage = empty(containerImage) ? builtImage : containerImage

// --------------------------------------------------------- container config
// Endpoints and names only. The webhook URL is the sole secret and goes through
// the secret store below rather than the plain env array; FOUNDRY_API_KEY would
// belong there too if the Foundry client ever needs a key instead of the
// managed identity token provider (see app/config.py).
var baseEnv = [
  { name: 'ENVIRONMENT', value: appEnvironment }
  { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
  { name: 'COSMOS_ENDPOINT', value: cosmos.properties.documentEndpoint }
  { name: 'COSMOS_DATABASE', value: cosmosDb.name }
  { name: 'STORAGE_ACCOUNT_URL', value: storage.properties.primaryEndpoints.blob }
  { name: 'CONTRACTS_CONTAINER', value: contractsContainer.name }
  { name: 'SERVICEBUS_NAMESPACE', value: '${serviceBus.name}.servicebus.windows.net' }
  { name: 'INGEST_QUEUE', value: ingestQueue.name }
  { name: 'DOC_INTELLIGENCE_ENDPOINT', value: docIntelligence.properties.endpoint }
  { name: 'FOUNDRY_RESOURCE', value: foundryResource }
  { name: 'AZURE_OPENAI_ENDPOINT', value: azureOpenAiEndpoint }
  { name: 'PROMPT_VERSION', value: promptVersion }
]

// An empty secret value is rejected by the platform, so both the secret and the
// env entry referencing it are omitted when no webhook is configured. The
// scanner then falls back to LoggingNotifier, which is the documented default.
var webhookSecrets = empty(notifyWebhookUrl) ? [] : [
  { name: 'notify-webhook-url', value: notifyWebhookUrl }
]
var webhookEnv = empty(notifyWebhookUrl) ? [] : [
  { name: 'NOTIFY_WEBHOOK_URL', secretRef: 'notify-webhook-url' }
]

var containerEnvVars = concat(baseEnv, webhookEnv)

// API only. The jobs serve no HTTP surface, so bearer validation is meaningless
// to them — and shipping the tenant id somewhere it is never read just widens
// what has to be kept in step. Both values or neither: app/auth.py installs the
// middleware only when both are present, so a half-configured revision is an
// unauthenticated one.
var entraEnv = (empty(entraTenantId) || empty(entraAudience)) ? [] : [
  { name: 'ENTRA_TENANT_ID', value: entraTenantId }
  { name: 'ENTRA_AUDIENCE', value: entraAudience }
]

var apiEnvVars = concat(containerEnvVars, entraEnv)

var registryConfig = [
  {
    server: containerRegistry.properties.loginServer
    identity: identity.id
  }
]

// -------------------------------------------------------------- the api app
// `azd-service-name: api` is load-bearing: it is the only thing tying this
// resource to the `api` service in azure.yaml, and how `azd deploy` knows which
// container app to push the built image to.
resource apiApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${prefix}-api'
  location: location
  tags: union(tags, { 'azd-service-name': 'api' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    environmentId: containerEnv.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      registries: registryConfig
      secrets: webhookSecrets
    }
    template: {
      containers: [
        {
          name: 'api'
          image: apiImage
          env: apiEnvVars
          resources: { cpu: json('0.5'), memory: '1Gi' }
          probes: [
            {
              // Liveness only, and /healthz touches no dependency — a Cosmos
              // blip should surface as a 503 on the real endpoints, not recycle
              // otherwise-healthy replicas. See app/api/health.py.
              type: 'Liveness'
              httpGet: { path: '/healthz', port: 8000 }
              initialDelaySeconds: 10
              periodSeconds: 30
            }
          ]
        }
      ]
      scale: {
        // Scale to zero: an idle API costs nothing but a cold start.
        minReplicas: 0
        maxReplicas: 3
        rules: [
          {
            name: 'http'
            http: { metadata: { concurrentRequests: '20' } }
          }
        ]
      }
    }
  }
  dependsOn: [acrPullRole]
}

// --------------------------------------------------------------- jobs
// Both jobs run the same image as the API and differ only in their command,
// so there is one build and one thing to keep patched. They are intentionally
// not azd services: azd builds one image per service, and three services over
// one Dockerfile would mean three pushes of identical bytes.

// Queue-triggered: KEDA starts an execution per batch of messages and the job
// drains and exits, so nothing is billed while the queue is empty.
resource ingestJob 'Microsoft.App/jobs@2024-03-01' = {
  name: '${prefix}-ingest'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    environmentId: containerEnv.id
    configuration: {
      triggerType: 'Event'
      // Generous: a large contract is Document Intelligence plus an LLM call
      // per batch, and being killed mid-extraction leaves a FAILED row that a
      // human has to look at.
      replicaTimeout: 1800
      replicaRetryLimit: 1
      registries: registryConfig
      secrets: webhookSecrets
      eventTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
        scale: {
          minExecutions: 0
          maxExecutions: 5
          pollingInterval: 30
          rules: [
            {
              name: 'servicebus-queue'
              type: 'azure-servicebus'
              metadata: {
                queueName: ingestQueue.name
                namespace: serviceBus.name
                messageCount: '1'
              }
              // `bicep build` warns BCP037 here: the property is real in the
              // Microsoft.App REST API — it is how KEDA authenticates to
              // Service Bus without a connection string — but missing from the
              // generated Bicep type. Expected, not a bug.
              identity: identity.id
            }
          ]
        }
      }
    }
    template: {
      containers: [
        {
          name: 'ingest'
          image: jobImage
          command: ['python', '-m', 'app.jobs.ingest_worker']
          env: containerEnvVars
          resources: { cpu: json('0.5'), memory: '1Gi' }
        }
      ]
    }
  }
  dependsOn: [acrPullRole]
}

// Cron-triggered: the daily obligation scan. UTC — Container Apps cron has no
// timezone field, so a local-time schedule would silently drift with DST.
resource scannerJob 'Microsoft.App/jobs@2024-03-01' = {
  name: '${prefix}-scanner'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    environmentId: containerEnv.id
    configuration: {
      triggerType: 'Schedule'
      replicaTimeout: 900
      replicaRetryLimit: 1
      registries: registryConfig
      secrets: webhookSecrets
      scheduleTriggerConfig: {
        // 07:00 UTC daily — before a European working day starts, so an
        // overdue obligation is waiting rather than arriving mid-afternoon.
        cronExpression: '0 7 * * *'
        parallelism: 1
        replicaCompletionCount: 1
      }
    }
    template: {
      containers: [
        {
          name: 'scanner'
          image: jobImage
          command: ['python', '-m', 'app.jobs.obligation_scanner']
          env: containerEnvVars
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
        }
      ]
    }
  }
  dependsOn: [acrPullRole]
}

// ------------------------------------------------------- role assignments
// Built-in role definition ids.
var cosmosDataContributorId = '00000000-0000-0000-0000-000000000002'
var blobDataContributorId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var serviceBusDataOwnerId = '090c5cfd-751d-490a-894a-3ce6f1109419'
var cognitiveServicesUserId = 'a97b65f3-24c7-4388-baec-2e87135dc908'
var acrPullId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

// Cosmos data-plane RBAC is its own resource type, not a standard role assignment.
resource cosmosRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = {
  parent: cosmos
  name: guid(cosmos.id, identity.id, cosmosDataContributorId)
  properties: {
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/${cosmosDataContributorId}'
    principalId: identity.properties.principalId
    scope: cosmos.id
  }
}

resource blobRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, identity.id, blobDataContributorId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions', blobDataContributorId
    )
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource serviceBusRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: serviceBus
  name: guid(serviceBus.id, identity.id, serviceBusDataOwnerId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions', serviceBusDataOwnerId
    )
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource docIntelligenceRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: docIntelligence
  name: guid(docIntelligence.id, identity.id, cognitiveServicesUserId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions', cognitiveServicesUserId
    )
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Without this the API and both jobs cannot pull their own image — admin user
// is off and there is no registry password anywhere. Every container resource
// above depends on it explicitly so the grant exists before the first pull.
resource acrPullRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: containerRegistry
  name: guid(containerRegistry.id, identity.id, acrPullId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions', acrPullId
    )
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ------------------------------------------- developer data-plane access
// Local auth is disabled on Cosmos, Storage and Service Bus, so there is no key
// to fall back on: without these a developer cannot read the data their own
// deployment writes. Skipped entirely when principalId is empty.
resource devCosmosRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = if (!empty(principalId)) {
  parent: cosmos
  name: guid(cosmos.id, principalId, cosmosDataContributorId)
  properties: {
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/${cosmosDataContributorId}'
    principalId: principalId
    scope: cosmos.id
  }
}

resource devBlobRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  scope: storage
  name: guid(storage.id, principalId, blobDataContributorId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions', blobDataContributorId
    )
    principalId: principalId
    principalType: principalType
  }
}

resource devServiceBusRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  scope: serviceBus
  name: guid(serviceBus.id, principalId, serviceBusDataOwnerId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions', serviceBusDataOwnerId
    )
    principalId: principalId
    principalType: principalType
  }
}

resource devDocIntelligenceRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  scope: docIntelligence
  name: guid(docIntelligence.id, principalId, cognitiveServicesUserId)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions', cognitiveServicesUserId
    )
    principalId: principalId
    principalType: principalType
  }
}

// ------------------------------------------------------------------ output
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = containerRegistry.properties.loginServer
output AZURE_CONTAINER_REGISTRY_NAME string = containerRegistry.name
output AZURE_CONTAINER_APPS_ENVIRONMENT_ID string = containerEnv.id
output AZURE_CONTAINER_APPS_ENVIRONMENT_NAME string = containerEnv.name

output SERVICE_API_NAME string = apiApp.name
output SERVICE_API_ENDPOINT_URL string = 'https://${apiApp.properties.configuration.ingress.fqdn}'

output AZURE_CLIENT_ID string = identity.properties.clientId
output COSMOS_ENDPOINT string = cosmos.properties.documentEndpoint
output COSMOS_DATABASE string = cosmosDb.name
output STORAGE_ACCOUNT_URL string = storage.properties.primaryEndpoints.blob
output SERVICEBUS_NAMESPACE string = '${serviceBus.name}.servicebus.windows.net'
output DOC_INTELLIGENCE_ENDPOINT string = docIntelligence.properties.endpoint
