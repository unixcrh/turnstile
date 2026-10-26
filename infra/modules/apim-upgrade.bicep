targetScope = 'resourceGroup'

param apimName string
param apiId string
param sourceRevision string
param revision string
@allowed([
  'prepare'
  'promote'
])
param stage string
param createRevision bool
param initializeImagePolicy bool
@secure()
param apiProperties object
@secure()
param parentPolicy string
@secure()
param imageOperationProperties object
param fixedOperations array = []

resource apim 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimName
}

resource api 'Microsoft.ApiManagement/service/apis@2024-05-01' existing = {
  parent: apim
  name: apiId
}

resource candidate 'Microsoft.ApiManagement/service/apis@2024-05-01' = if (stage == 'prepare') {
  parent: apim
  name: '${apiId};rev=${revision}'
  properties: union(apiProperties, {
    apiRevisionDescription: 'Turnstile infrastructure upgrade ${revision}'
  }, createRevision ? {
    sourceApiId: '${api.id};rev=${sourceRevision}'
  } : {})
}

resource imageOperation 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = if (stage == 'prepare' && initializeImagePolicy) {
  parent: candidate
  name: 'images-generations'
  properties: imageOperationProperties
}

resource imagePolicy 'Microsoft.ApiManagement/service/apis/operations/policies@2024-05-01' = if (stage == 'prepare' && initializeImagePolicy) {
  parent: imageOperation
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../policies/provider-neutral-images-policy.xml')
  }
}

@batchSize(1)
resource fixedOperation 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = [for operation in fixedOperations: if (stage == 'prepare') {
  parent: candidate
  name: operation.name
  properties: {
    displayName: operation.name
    method: operation.method
    urlTemplate: operation.path
    templateParameters: []
    responses: []
  }
}]

resource fixedOperationPolicy 'Microsoft.ApiManagement/service/apis/operations/policies@2024-05-01' = [for (operation, index) in fixedOperations: if (stage == 'prepare') {
  parent: fixedOperation[index]
  name: 'policy'
  properties: {
    format: 'xml'
    value: operation.policy
  }
}]

resource parent 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = if (stage == 'prepare') {
  parent: candidate
  name: 'policy'
  properties: {
    format: empty(fixedOperations) ? 'rawxml' : 'xml'
    value: parentPolicy
  }
  dependsOn: [
    imagePolicy
    fixedOperationPolicy
  ]
}

resource promotion 'Microsoft.ApiManagement/service/apis/releases@2024-05-01' = if (stage == 'promote') {
  parent: api
  name: 'infrastructure-${revision}'
  properties: {
    apiId: '${api.id};rev=${revision}'
    notes: 'Turnstile verified infrastructure upgrade ${revision}'
  }
}
