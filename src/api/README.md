# Typed API schema

`schema.d.ts` is generated from the backend's OpenAPI document. Don't edit it
by hand. After changing a backend route, regenerate it and commit the result:

```bash
npm run api:types
```

CI regenerates it and fails if the committed file is out of date.

Routes don't declare `response_model` yet, so response bodies are typed as
unknown. Request bodies, path and query parameters are typed now. As routes
gain response models, those types appear here with no frontend change.
