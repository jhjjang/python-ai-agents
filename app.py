from flask import Flask, request, jsonify
from flask_cors import CORS
import mysql.connector
from mysql.connector import pooling
import openai
import os
from dotenv import load_dotenv

# 환경 변수 로드
load_dotenv()

app = Flask(__name__)
CORS(app)

# MySQL 연결 풀 설정
db_config = {
    'host': os.getenv('DB_HOST', 'localhost'),
    'user': os.getenv('DB_USER', 'mcp_readonly'),
    'password': os.getenv('DB_PASSWORD', 'secure_password'),
    'database': os.getenv('DB_NAME', 'your_database'),
}

connection_pool = pooling.MySQLConnectionPool(
    pool_name="mcp_pool",
    pool_size=10,
    **db_config
)

# OpenAI API 설정
openai.api_key = os.getenv('OPENAI_API_KEY')



def extract_sql_query(response_text):
    """
    ChatGPT 응답에서 SQL 쿼리만 추출

    응답 형식:
    1. 코드 블록으로 감싸진 경우: ```sql SELECT * FROM users ```
    2. "SQL 쿼리:"로 시작하는 문장
    3. 일반 텍스트
    """

    # 코드 블록 확인 (```sql과 ``` 사이의 내용)
    import re
    sql_pattern = re.compile(r'```(?:sql)?\s*(.*?)\s*```', re.DOTALL)
    matches = sql_pattern.findall(response_text)

    if matches:
        # 코드 블록이 여러 개 있을 경우 첫 번째 것만 사용
        return matches[0].strip()

    # "SQL 쿼리:" 또는 "쿼리:" 형식 확인
    query_pattern = re.compile(r'(?:SQL\s+)?(?:쿼리|query):\s*(.*?)(?:\n\n|$)', re.DOTALL | re.IGNORECASE)
    matches = query_pattern.findall(response_text)

    if matches:
        return matches[0].strip()

    # 위 패턴이 모두 없으면 전체 응답을 SQL로 간주
    return response_text.strip()

def get_database_schema(connection_pool):
    """데이터베이스 스키마 정보를 가져오는 함수"""
    connection = connection_pool.get_connection()
    cursor = connection.cursor(dictionary=True)
    schema_info = {}

    try:
        # 테이블 목록 가져오기
        cursor.execute("SHOW TABLES")
        tables = [table[f'Tables_in_{db_config["database"]}'] for table in cursor.fetchall()]

        # 각 테이블의 컬럼 정보 가져오기
        for table in tables:
            cursor.execute(f"DESCRIBE `{table}`")
            columns = cursor.fetchall()

            # 테이블 관계 정보 가져오기 (외래 키)
            cursor.execute(f"""
                SELECT
                    TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
                FROM
                    INFORMATION_SCHEMA.KEY_COLUMN_USAGE
                WHERE
                    REFERENCED_TABLE_SCHEMA = '{db_config["database"]}'
                    AND TABLE_NAME = '{table}'
            """)
            relations = cursor.fetchall()

            schema_info[table] = {
                'columns': columns,
                'relations': relations
            }

    except mysql.connector.Error as err:
        print(f"데이터베이스 스키마 정보 가져오기 오류: {err}")
    finally:
        cursor.close()
        connection.close()

    return schema_info

# 글로벌 변수로 스키마 정보 저장
schema_info = None

# @app.before_first_request
# def initialize():
#     global schema_info
#     schema_info = get_database_schema(connection_pool)
#     print("데이터베이스 스키마 정보가 로드되었습니다.")
#     # print(schema_info)

@app.route('/query', methods=['POST'])
def execute_query():
    try:
        data = request.json
        user_query = data.get('query')
        user_id = data.get('user_id')

        user_query = f" member_id 가 {user_id} 의 {user_query}"
        print(f"사용자 쿼리: {user_query}")

        if not user_query:
            return jsonify({'error': '쿼리가 제공되지 않았습니다'}), 400

        # 1단계: OpenAI를 사용하여 자연어를 SQL로 변환
        validate_response = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": """
                    You are an AI that converts natural language into SQL.

                    Here is the database schema:
                    Table: onch_product_data
                    - num
                    - prd_code
                    - member_id
                    - product_id
                    - wdate

                    Table: member_order_data
                    - num
                    - prd_code
                    - member_id
                    - member_nm
                    - p_mem_id
                    - option_nm
                    - prd_num
                    - cus_pay
                    - prd_pay
                    - onch_pay
                    - wdate

                    당신은 SQL 쿼리 검증기입니다. 사용자의 자연어 요청을 검토하고 안전한 읽기 전용 SQL 쿼리로 변환하세요. DELETE, UPDATE, DROP, CREATE 문은 허용하지 마세요.
                    """
                },
                {
                    "role": "user",
                    "content": f'다음 요청을 안전한 MySQL 쿼리로 변환해주세요: "{user_query}"'
                }
            ]
        )

        sql_query = validate_response.choices[0].message.content.strip()
        extract_sql_query_response = extract_sql_query(sql_query)
        # print(f"{extract_sql_query_response}")

        # SQL 명령어 안전성 검사
        forbidden_commands = ['DELETE', 'UPDATE', 'INSERT', 'DROP', 'CREATE', 'ALTER', 'TRUNCATE']
        if any(cmd in sql_query.upper() for cmd in forbidden_commands):
            return jsonify({'error': '허용되지 않는 SQL 명령어가 포함되어 있습니다'}), 403


        # 2단계: 쿼리 실행
        connection = connection_pool.get_connection()
        cursor = connection.cursor(dictionary=True)
        print(extract_sql_query_response)
        cursor.execute(extract_sql_query_response)
        results = cursor.fetchall()
        cursor.close()
        connection.close()

        print(results)
        # 3단계: 결과를 자연어로 해석
        interpret_response = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "당신은 SQL 쿼리 결과 해석기입니다. SQL 쿼리와 결과를 받아서 사용자가 이해하기 쉬운 자연어로 설명하세요."
                },
                {
                    "role": "user",
                    "content": f'SQL 쿼리: {sql_query}\n\n결과: {results}\n\n이 결과를 html 테이블 코딩으로 변환해서 설명을 제외하고 데이터만 깔끔하게 보여주세요. '
                }
            ]
        )

        interpretation = interpret_response.choices[0].message.content

        return jsonify({
            'query': sql_query,
            'results': results,
            'interpretation': interpretation
        })

    except mysql.connector.Error as db_err:
        return jsonify({'error': f'데이터베이스 오류: {str(db_err)}'}), 500
    except openai.error.OpenAIError as ai_err:
        return jsonify({'error': f'OpenAI API 오류: {str(ai_err)}'}), 500
    except Exception as e:
        return jsonify({'error': f'서버 오류: {str(e)}'}), 500





if __name__ == '__main__':
    port = int(os.getenv('PORT', 3000))
    app.run(host='0.0.0.0', port=port, debug=True)