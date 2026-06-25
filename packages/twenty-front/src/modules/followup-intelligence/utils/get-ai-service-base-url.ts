const getDefaultAiServiceUrl = () => {
  if (
    window.location.hostname.endsWith('localhost') ||
    window.location.hostname.endsWith('127.0.0.1')
  ) {
    return `http://${window.location.hostname}:8001`;
  }

  return `${window.location.protocol}//${window.location.hostname}${
    window.location.port ? `:${window.location.port}` : ''
  }`;
};

export const REACT_APP_AI_SERVICE_URL =
  window._env_?.REACT_APP_AI_SERVICE_URL ?? getDefaultAiServiceUrl();
