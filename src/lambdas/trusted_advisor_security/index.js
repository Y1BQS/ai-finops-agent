const { Readable } = require('stream');
const { TrustedAdvisorClient, ListRecommendationsCommand, GetRecommendationCommand } = require("@aws-sdk/client-trustedadvisor");

const client = new TrustedAdvisorClient({});
const CHUNK_SIZE = 25 * 1024;

const streamFunctionResponse = async (function_response, responseStream) => {
  const jsonResponse = JSON.stringify(function_response);
  const requestStream = Readable.from(jsonResponse);
  for await (const chunk of requestStream) {
    if (chunk.length > CHUNK_SIZE) {
      for (let i = 0; i < chunk.length; i += CHUNK_SIZE) {
        await responseStream.write(chunk.slice(i, i + CHUNK_SIZE));
      }
    } else {
      await responseStream.write(chunk);
    }
  }
  responseStream.end();
};

module.exports.handler = awslambda.streamifyResponse(async (event, responseStream, context) => {
  try {
    responseStream.setContentType("application/json");
    const get_security_findings = async () => {
      const findings = [];
      let nextToken = undefined;
      do {
        const input = { maxResults: 200, pillar: "security", status: "warning" };
        if (nextToken) input.nextToken = nextToken;
        const response = await client.send(new ListRecommendationsCommand(input));
        for (const rec of response.recommendationSummaries || []) {
          try {
            const detail = await client.send(new GetRecommendationCommand({ recommendationIdentifier: rec.arn }));
            const resourceAggregates = rec.resourcesAggregates || {};
            findings.push({
              recommendationIdentifier: rec.arn,
              checkName: rec.name,
              checkId: rec.id,
              status: rec.status,
              description: detail.recommendation?.description || '',
              recommendedAction: detail.recommendation?.recommendedActions?.[0]?.description || '',
              resourceCount: resourceAggregates.errorCount + resourceAggregates.warningCount,
              resources: detail.recommendation?.resources?.map(r => ({
                resourceId: r.resourceId || '',
                region: r.metadata?.region || '',
                status: r.status,
                metadata: r.metadata || {}
              })) || []
            });
          } catch (e) {
            console.error("Error processing recommendation:", rec.name, e);
          }
        }
        nextToken = response.nextToken;
      } while (nextToken);
      return findings;
    };
    const findings = await get_security_findings();
    const checks = {
      summary: { totalFindings: findings.length, lastUpdated: new Date().toISOString().replace('T', ' ').substr(0, 19) },
      findings
    };
    const function_response = {
      messageVersion: "1.0",
      response: {
        actionGroup: event.actionGroup || '',
        function: event.function,
        functionResponse: { responseBody: { TEXT: { body: JSON.stringify({ checks }) } } }
      }
    };
    await streamFunctionResponse(function_response, responseStream);
  } catch (e) {
    console.error("Unexpected error:", e);
    const error_response = {
      messageVersion: "1.0",
      response: {
        actionGroup: event.actionGroup || '',
        function: event.function,
        responseState: 'REPROMPT',
        functionResponse: { responseBody: { TEXT: { body: JSON.stringify({ error: e.message }) } } }
      }
    };
    await streamFunctionResponse(error_response, responseStream);
  }
});
