# 第 8 章：设计 URL Shortener

## 简介
本章讨论类似 TinyURL 的 URL 缩短服务的设计。系统的主要目标包括 **URL 缩短**、**重定向**和**高可扩展性**，以处理大量流量。

### 要求
- 缩短后的 URL 必须**唯一**且**尽可能短**。
- 每天处理 **100 million 次 URL 生成**，支持 10 年的容量。
- 支持**高效的读取操作**，读写比为 10:1。
- 存储 365 billion 条记录，需要约 **365 TB** 的存储空间，覆盖 10 年。

---

## 步骤 1：高层设计

### API 端点
1. **URL 缩短：**  
   - 端点：`POST api/v1/data/shorten`  
   - 参数：`{longUrl: longURLString}`  
   - 返回：`shortURL`

2. **URL 重定向：**  
   - 端点：`GET api/v1/shortUrl`  
   - 返回用于重定向的 `longURL`。

    <p align="center">
    <img src="./images/url-redirection.png" alt="URL 重定向" width="600">
    </p>

### URL 重定向
- **301 重定向：** 301 重定向表示请求的 URL 已“永久”移动到长 URL。浏览器会缓存响应，
后续对相同 URL 的请求将不会发送到 URL 缩短服务。
- **302 重定向：**临时重定向；适用于点击跟踪等分析。

### URL 缩短
<p align="center">
    <img src="./images/url-shortening.png" alt="URL 缩短" width="400">
</p>

- 使用**哈希函数**生成短 URL，将长 URL 映射到唯一的缩短版本。
- 哈希函数必须满足以下要求：
    - 每个 longURL 必须被哈希为一个 hashValue。
    - 每个 hashValue 都可以映射回 longURL。
    

---

## 步骤 2：深入设计

### 数据模型
将 `<shortURL, longURL>` 映射存储在关系型数据库中，以优化内存使用。表结构包括：
- `id`（主键）、
- `shortURL`、
- `longURL`。

    <img src="./images/table-schema.png" alt="表结构" width="300">

### 哈希函数
#### 1. Base 62 转换：
- 使用字符 `[0-9, a-z, A-Z]` 对数字进行编码，提供 **62 个可能的字符**。
- 进制转换是 URL Shortener 常用的另一种方法。
- 可以为短 URL 分配唯一 id，并将 ID 转换为 Base 62 以获得短 URL。
- 7 个字符的哈希最多支持 **3.5 trillion 个唯一 URL**，足以容纳 365 billion 个 URL。

**示例：**  
将 ID `2009215674938` 转换为 Base 62：
- `2009215674938` → `zn9edcu`。

#### 2. 哈希 + 冲突解决：
- 使用 CRC32、MD5 或 SHA-1 等哈希函数。

    <img src="./images/hash-function.png" alt="哈希函数" width="500">

- 一种方法是收集哈希值的前 7 个字符；但是，这种方法可能导致哈希冲突。
- 要解决冲突，递归地追加一个新的预定义字符串，直到不再发生冲突，但这可能代价高昂。
- 使用 **Bloom Filters** 解决冲突，以实现高效查找。

    <p align="center">
    <img src="./images/url-lookup.png" alt="URL 查找" width="500">
    </p>

### 比较

-  **哈希 + 冲突解决：**
    - 短 URL 长度固定
    - 不需要唯一 ID 生成器
    - 可能发生冲突，需要解决
    - 无法找到下一个可用的短 URL，因为它不依赖 ID

- **Base 62 转换**
    - 长度不是固定的，并且会随 ID 增长
    - 需要唯一 ID 生成器
    - 不可能发生冲突
    - 如果 ID 每次递增 1，则很容易找到下一个短 URL（可能存在安全问题）


---

### URL 缩短流程

<p align="center">
    <img src="./images/url-shortening-flow.png" alt="URL 缩短" width="500">
</p>

1. 检查数据库中是否存在 `longURL`。
2. 如果找到，则返回现有的 `shortURL`。
3. 否则：
   - 使用**分布式 ID 生成器**生成唯一 ID。
   - 使用 Base 62 将 ID 转换为 `shortURL`。
   - 将 `<id, shortURL, longURL>` 映射存储在数据库中。



---

### URL 重定向流程
<p align="center">
    <img src="./images/url-redirecting-flow.png" alt="URL 缩短" width="600">
</p>

1. 用户点击 `shortURL`。
2. 查询 `<shortURL, longURL>` 映射：
   - 首先检查**缓存**以实现更快的访问。
   - 如果缓存中没有，则查询数据库。
3. 将用户重定向到 `longURL`。


---

## 其他注意事项
### 速率限制器
- 通过设置每个 IP 的请求上限来防止滥用。

### 可扩展性
1. **Web 层：**无状态，可通过添加/移除 Web 服务器进行扩展。
2. **数据库层：**使用复制和分片。

### 分析
- 收集点击率、来源和时间戳等数据，以获取业务洞察。

### 高可用性和可靠性
- 使用数据库复制和容错设计，确保服务一致且可靠。
 
