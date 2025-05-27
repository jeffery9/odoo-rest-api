# Odoo REST API

This is a module which exposes Odoo 18 as a REST API with proper JSON responses and Bearer token authentication.

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/jeffery9/odoo-rest-api)

## Installing

- Download this module and put it to your Odoo addons directory
- Install requirements with pip install -r requirements.txt
- Ensure you have API keys configured in Odoo for authentication

## Getting Started

### Authentication

This API uses Bearer token authentication with API keys. You need to include the Authorization header with your API key in all requests.

```python
import json  
import requests  
  
# API endpoints  
BASE_URL = 'http://localhost:8069'  
API_BASE = f'{BASE_URL}/api/v1'  
  
# Set up headers with API key  
headers = {  
    'Content-Type': 'application/json',  
    'Authorization': 'Bearer your_api_key_here'  
}  
  
# Example 1: Get users  
USERS_URL = f'{API_BASE}/res.users'  
  
res = requests.get(USERS_URL, headers=headers)  
response_data = res.json()  
  
if response_data['success']:  
    users = response_data['data']['records']  
    print(f"Found {len(users)} users")  
else:  
    print(f"Error: {response_data['error']['message']}")  
  
# Example 2: Get products with field selection  
PRODUCTS_URL = f'{API_BASE}/product.product'  
params = {'query': '{id, name}'}  
  
res = requests.get(PRODUCTS_URL, params=params, headers=headers)  
print(res.json())
```

## API Response Format

All API responses follow a consistent JSON format:

Success Response:

```json
{  
    "success": true,  
    "message": "Operation completed successfully",  
    "data": {  
        // Response data here  
    },  
    "timestamp": "2024-01-01T12:00:00.000Z"  
}
```

Error Response:

```json
{  
    "success": false,  
    "error": {  
        "code": 400,  
        "message": "Error description",  
        "type": "ValidationError",  
        "details": "Detailed error information"  
    },  
    "timestamp": "2024-01-01T12:00:00.000Z"  
}
```

## Allowed HTTP Methods

## 1. GET

### Get All Records: GET /api/v1/{model}

#### Parameters

- query (optional): Field selection using query syntax
- filter (optional): JSON-encoded domain filter
- order (optional): JSON-encoded order specification
- limit (optional): Maximum number of records (max 1000)
- page_size (optional): Records per page for pagination (max 1000)
- page (optional): Page number for pagination

#### Examples

Basic request:

```python
GET /api/v1/res.users?query={id, name}  

```

Response:

```json
{  
    "success": true,  
    "message": "Records retrieved successfully",  
    "data": {  
        "records": [  
            {"id": 2, "name": "Administrator"},  
            {"id": 6, "name": "Demo User"}  
        ],  
        "pagination": {  
            "count": 2,  
            "total_count": 2,  
            "current_page": 1,  
            "total_pages": 1,  
            "page_size": null,  
            "prev_page": null,  
            "next_page": null  
        }  
    },  
    "timestamp": "2024-01-01T12:00:00.000Z"  
}
```

Nested field selection:

```python
GET /api/v1/res.users?query={id, name, company_id{name}}  

```

Filtering:

```python
GET /api/v1/product.template?filter=[["list_price", ">", 100]]&query={id, name, list_price}  

```

Pagination:

```python
GET /api/v1/product.template?page_size=10&page=2&query={id, name}  

```

### Get Single Record: GET /api/v1/{model}/{id}

#### Parameters

- query (optional): Field selection

#### Example

```python
GET /api/v1/product.template/95?query={id, name, list_price}  

```

## 2. POST

### Create Record: POST /api/v1/{model}/

#### Request Body

```json
{  
    "data": {  
        "name": "New Product",  
        "list_price": 100.0  
    },  
    "context": {  
        "lang": "en_US"  
    }  
}
```

#### Response

```json
{  
    "success": true,  
    "message": "Record created successfully",  
    "data": {  
        "id": 123,  
        "name": "New Product",  
        "list_price": 100.0  
    },  
    "timestamp": "2024-01-01T12:00:00.000Z"  
}
```

### Batch Create: POST /api/v1/{model}/batch

#### Request Body

```json
{  
    "data": [  
        {"name": "Product 1", "list_price": 50.0},  
        {"name": "Product 2", "list_price": 75.0}  
    ]  
}
```

## 3. PUT

### Update Single Record: PUT /api/v1/{model}/{id}/

#### Request Body

```json
{  
    "data": {  
        "name": "Updated Product Name",  
        "list_price": 150.0  
    }  
}
```

### Update Multiple Records: PUT /api/v1/{model}/

#### Request Body with Filter

```json
{  
    "filter": [["category_id", "=", 5]],  
    "data": {  
        "active": false  
    }  
}
```

#### Request Body with IDs (via URL params)

```python
PUT /api/v1/product.template/?id=[1,2,3]  

```

```json
{  
    "data": {  
        "list_price": 200.0  
    }  
}
```

### Relational Field Operations

For one2many and many2many fields, you can use these operations:

```json
{  
    "data": {  
        "category_ids": {  
            "link": [1, 2, 3],  
            "unlink": [4, 5],  
            "delete": [6],  
            "set": [1, 2, 7, 8]  
        }  
    }  
}
```

Operations:

- link: Add existing records to the relation
- unlink: Remove records from relation (but don't delete them)
- delete: Remove and delete records permanently
- set: Replace all related records with the specified list
- create: Create new records and link them
- update: Update existing related records
- clear: Remove all related records

## 4. DELETE

### Delete Single Record: DELETE /api/v1/{model}/{id}/

#### Response

```json
{  
    "success": true,  
    "message": "Record deleted successfully",  
    "data": {  
        "success": true,  
        "message": "Record deleted successfully"  
    },  
    "timestamp": "2024-01-01T12:00:00.000Z"  
}
```

### Delete Multiple Records: DELETE /api/v1/{model}/?id=[1,2,3]

## 5. Function Calls

### Call Model Method: POST /api/v1/object/{model}/{function}

#### Request Body

```json
{  
    "args": ["arg1", "arg2"],  
    "kwargs": {  
        "key1": "value1",  
        "key2": "value2"  
    }  
}
```

### Call Record Method: POST /api/v1/object/{model}/{id}/{function}

#### Request Body

```json
{  
    "args": [],  
    "kwargs": {  
        "force": true  
    }  
}
```

## Additional Endpoints

### Advanced Search: POST /api/v1/{model}/search

```json
{  
    "domain": [["name", "ilike", "test"]],  
    "fields": ["id", "name", "email"],  
    "limit": 50,  
    "offset": 0,  
    "order": "name ASC"  
}
```

### Get Model Fields: GET /api/v1/{model}/fields

Returns field definitions for the specified model.

### Get File/Binary Field: GET /api/v1/{model}/{id}/{field}

Downloads binary field content.

### Health Check: GET /api/v1/health

Check API status (no authentication required).

### API Information: GET /api/v1/info

Get API version and endpoint information.

## Query Language

The query parameter supports a powerful field selection syntax:

- {id, name} - Select specific fields
- {*} - Select all fields
- {-password} - Exclude specific fields
- {user{name, email}} - Nested field selection
- {*, user{-password}} - All fields with nested exclusions

## Error Handling

The API returns appropriate HTTP status codes:

- 200 - Success
- 400 - Bad Request (validation errors)
- 404 - Not Found
- 500 - Internal Server Error

All errors include detailed error information in the response body.

## Authentication Setup

1. Create API keys in Odoo: Settings → Users & Companies → API Keys
2. Set the scope to "odoo.api"
3. Use the generated key in the Authorization header: Bearer your_api_key

## Notes

- All timestamps are in ISO 8601 format
- Maximum limit per request is 1000 records
- Binary fields are base64 encoded in JSON responses
- The API supports Odoo's domain filter syntax for complex queries
- Relational fields return IDs by default, use nested queries for full objects
