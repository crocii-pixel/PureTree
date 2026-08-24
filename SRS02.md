# 소프트웨어 요구사항 명세서 (SRS)

## 프로젝트: PureTree 기반 하이브리드 비즈니스 엔진 기반 구축 (Hybrid In-Memory Business Foundation)

**버전:** 1.4 (RDB Skeleton Foundation) **작성일:** 2026-04-21

---

## 1. 시스템 철학 및 개요 (Introduction)

### 1.1 배경 및 한계

기존의 PureTree는 완전한 자유도를 지향했으나, 이는 역으로 강제되는 원칙이 없어 "타입의 남발", "모호한 구조 설계", "잘못된 사용" 등의 부작용을 낳을 우려가 있었다.
PureTree는 유지하고 이를 확장하여 RDBTree.h 파일을 만들고, CTreeDB가 PureTree를 상속받아 사용하도록 한다.(TimerTree.h 참고)

### 1.2 핵심 해결책: RDB 스켈레톤 (RDB Skeleton Foundation)

이러한 한계를 극복하기 위해, **RDBMS의 탄탄한 스키마 체계를 PureTree 구조 설계의 뼈대(Skeleton)로 삼는다**.
엔진은 단순히 인메모리 트리로 끝나는 것이 아니라, 추후 ERP 등 **복잡한 비즈니스 시스템과 원활하게 호환 및 연계될 수 있도록 탄탄한 기반을 마련**하는 것을 목적으로 한다.

Tree는 RDB 기반의 정형화된 구조를 임포트하여 기본 골격으로 활용하며, 그 위에서 고유의 자유로운 관계(영향력, 복합 링크)를 부가적으로 확장할 수 있는 유연성을 제공한다. 이를 통해 Tree 구조를 비즈니스 계층(Business Tier)으로 안전하게 확장 사용할 수 있는 기틀을 닦는다.

---

## 2. 핵심 요구사항 (Core Requirements)

### 2.1 데이터 내재화 기반 유연성 (Data Internalization)

* **레코드 내재화 (In-Memory Capability):** 잦은 DB 액세스로 인한 퍼포먼스 하락을 방지하고 PK만으로 파악하기 어려운 노드의 정체성을 유지하기 위해, 단순 스키마뿐만 아니라 실제 레코드 데이터를 바탕으로 Tree 노드를 운용(내재화)할 수 있는 기반 기능을 갖춘다.
* **Handle ↔ PK 매핑:** DB의 기본키(PK) 체계를 존중하되, 성능을 위해 인메모리 상에서는 CSlotMap의 O(1) 팩킹 핸들과 DB PK 간의 양방향 매핑 테이블(Translation Map)을 운용한다.

### 2.2 직관적 타입 명명 규칙 (Prefix Naming)

* **`db_` 접두어 도입:** 복잡한 대분류 메타데이터를 추가하는 대신, `db_Customer`, `db_Order`와 같이 노드 타입명에 접두어(`db_`)를 사용하여 직관적으로 RDB 동기화 대상임을 판단할 수 있게 한다.
* 접두어가 없는 타입은 Tree 내에서만 존재하는 고유 로직/관계 노드로 간주된다.

### 2.3 엄격한 타입 레지스트리 관리 (Type Registry)

* **중복 생성 방지:** 다중 사용자 및 수많은 노드 환경에서의 오남용을 막고 일관성을 보장하기 위해, `nodeTypes`, `linkTypes` 테이블(Registry)을 별도로 운용한다. 모든 타입은 문자열이 아닌 예약된 정수 ID로 관리된다.

### 2.4 비트마스크 배열 기반 고속 필터링 (Fast Bitmask Filter)

* **배경:** 복잡한 그래프 순회에서 빈번한 타입 검사(`if (type == A || type == B)`)는 큰 성능 병목이 된다.
* **구현:** 타입 레지스트리의 전체 크기만큼 할당된 배열(또는 Bitmask) 필터를 생성하고, 검색할 타입의 인덱스만 `1`(나머지는 `0`)로 설정한다.
* **효과:** 순회 중 `if (filter[node.type])` 형태의 단일 인덱스 참조 연산만으로, 다중 조건 필터링을 최소한의 오버헤드로 초고속 마킹/조회할 수 있다.

---

## 3. 부가 요구사항 (Supplementary Accommodations)

### 3.1 영속성 동기화 (Dirty Flag & Lazy DB Write)

* 노드나 관계가 변경되면 `CPublisher`를 통해 전파되고 `Dirty` 플래그가 마킹된다.
* 엔진은 유휴 상태나 명시적인 `flush` 호출 시 Dirty 노드와 Handle↔PK 매핑 정보를 DB에 Lazy하게 반영한다. (별도 WAL을 자체 구현하지 않음)

### 3.2 플랫폼별 직렬화 및 구조체 파싱 (Serialization)

* 노드 필드 데이터의 레이아웃과 외부 컴포넌트(뷰 등) 전달을 위해 기존의 `CPropParserStruct` 와 `CPropBinSerializer` 파이프라인을 그대로 활용한다.
* 플랫폼 구애 없이(포인터 크기, 패딩 등) 직렬화기가 처리를 전담하므로, 엔진 자체의 유연성이 보장된다.
